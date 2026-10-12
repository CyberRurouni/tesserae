"""
active.py — which family a run belongs to, and the one-time migration.

A run needs to name exactly one family. Until the config UI grows a family
switcher there is no user-facing choice, so the project carries an explicit
pointer rather than guessing "the newest family" — a guess would silently
rebind someone's verdicts to the wrong lens after an edit.

Bootstrap, when no family exists yet:
  1. create one from the current about_us.txt / additional_filters.txt
  2. place it in the taxonomy with the classifier (scope may be left empty if
     the model is unavailable — an unclassified family still works, it just
     cannot seed from or be found by related-family discovery)
  3. mirror the texts back to the profile files so the existing one-page UI
     keeps behaving exactly as before

Migration, when a legacy hash namespace exists:
  The old key space was tesserae:seen_ad:{category}:{about_hash}:{filter_hash}:{ad_id}
  and a verdict key carried no outcome — only "a final verdict was reached".
  Outcomes are recovered from stored_ads.json: ids present there were accepted,
  everything else was judged and set aside. Losing this would re-collect every
  ad the user has already seen.
"""

import json
import logging
from pathlib import Path

from genesis.config import (
    ABOUT_US_PATH,
    ADDITIONAL_FILTERS_PATH,
    FAMILIES_DIR,
)

from . import store

logger = logging.getLogger(__name__)

ACTIVE_FILE = FAMILIES_DIR / "active.json"

LEGACY_SEEN_PREFIX = "tesserae:seen_ad"
LEGACY_LEGACY_TTL = 90 * 24 * 3600


# ── the pointer ──────────────────────────────────────────────────────────────
def read_active() -> dict | None:
    if not ACTIVE_FILE.exists():
        return None
    try:
        data = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("❌ active.json unreadable (%s)", exc)
        return None
    return data if data.get("family_id") else None


def set_active(family_id: str, request_family_id: str | None = None) -> None:
    ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(
        json.dumps(
            {"family_id": family_id, "request_family_id": request_family_id}, indent=2
        ),
        encoding="utf-8",
    )


def active_family() -> store.Family | None:
    data = read_active()
    return store.get_family(data["family_id"]) if data else None


def active_request_family() -> store.RequestFamily | None:
    data = read_active()
    return store.get_request_family(data["request_family_id"]) if data and data.get("request_family_id") else None


# ── bootstrap ────────────────────────────────────────────────────────────────
async def ensure_active_family() -> store.Family:
    """
    The family a run should use, creating one on first use.

    Idempotent: once the pointer exists this is a file read plus, when the
    profile text has moved on, one classification call.
    """
    existing = active_family()
    if existing:
        return await sync_with_profile_file(existing)

    about = ABOUT_US_PATH.read_text(encoding="utf-8").strip() if ABOUT_US_PATH.exists() else ""
    request_text = (
        ADDITIONAL_FILTERS_PATH.read_text(encoding="utf-8").strip()
        if ADDITIONAL_FILTERS_PATH.exists()
        else ""
    )

    # Name it before creating, so the id reads well rather than being a raw first
    # line of the profile.
    about_label = _derive_about(about)
    try:
        from .classifier import classify_profile_change

        verdict = await classify_profile_change("", about)
        if verdict.about:
            about_label = verdict.about
    except Exception as exc:  # noqa: BLE001 - bootstrap must survive a model outage
        logger.warning("⚠️ Could not name the profile (%s) — using the first line", exc)

    family = store.create_family(about=about_label, profile_text=about)
    request_family = store.create_request_family(family.family_id, request_text)
    set_active(family.family_id, request_family.request_family_id)

    logger.info("👨‍👩‍👧 Bootstrapped family %s (about=%r)", family.family_id, family.about)
    return family


def _derive_about(profile_text: str) -> str:
    """
    A readable placeholder when the model has not named the family yet.

    Falls back to the opening line, trimmed on a word boundary. Real naming is
    the classifier's job; this only has to be recognisable in a list.
    """
    first = (profile_text.strip().splitlines() or [""])[0].strip().strip("\"'")
    if not first:
        return "Untitled family"
    words, out = first.split(), []
    for word in words:
        if sum(len(w) + 1 for w in out) + len(word) > 48:
            break
        out.append(word)
    return " ".join(out) or "Untitled family"


async def sync_with_profile_file(family: store.Family) -> store.Family:
    """
    Follow about_us.txt if it was edited outside these screens.

    The run pipeline reads the profile files, so if they move the profile has to
    move with them. Structural classification is NOT done here: that is the
    screens' job, with the user present to agree to it.
    """
    text = ABOUT_US_PATH.read_text(encoding="utf-8").strip() if ABOUT_US_PATH.exists() else ""
    if text and text != family.profile_text:
        family.profile_text = text
        store.save_family(family)
        logger.info("📝 Profile text changed outside the screens — followed for %s", family.family_id)
    return family
