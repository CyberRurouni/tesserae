"""
active.py — which profile a run belongs to, and under which request.

A run needs to name exactly one profile and one request: rejections and
deferrals are profile-scoped, so binding a run to nothing means re-judging
ads the user already parked. Rather than guessing "the newest profile", the
project carries an explicit pointer — a guess would silently rebind someone's
verdicts to the wrong profile after an edit.

Bootstrap, when no profile exists yet:
  1. create one from the current about_us.txt / additional_filters.txt
  2. name it with the classifier (the first line is the fallback, so a model
     outage still yields a usable profile)
  3. create its first request from additional_filters.txt and point at it
  4. mirror the texts back to the profile files so the existing one-page UI
     keeps behaving exactly as before

Self-heal: a pointer written before requests existed has no request id. That
is repaired in place rather than rejected, because the profile has exactly one
request and the answer is not ambiguous.
"""

import json
import logging
from pathlib import Path

from genesis.config import (
    ABOUT_US_PATH,
    ADDITIONAL_FILTERS_PATH,
    PROFILES_DIR,
)

from . import store

logger = logging.getLogger(__name__)

ACTIVE_FILE = PROFILES_DIR / "active.json"


# ── the pointer ──────────────────────────────────────────────────────────────
def read_active() -> dict | None:
    if not ACTIVE_FILE.exists():
        return None
    try:
        data = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("❌ active.json unreadable (%s)", exc)
        return None
    return data if data.get("profile_id") else None


def set_active(profile_id: str, request_id: str | None = None) -> None:
    ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(
        json.dumps(
            {"profile_id": profile_id, "request_id": request_id}, indent=2
        ),
        encoding="utf-8",
    )


def active_profile() -> store.Profile | None:
    data = read_active()
    return store.get_profile(data["profile_id"]) if data else None


def _only_request(profile_id: str) -> str | None:
    """The profile's sole request id, if it has exactly one."""
    requests = store.list_requests(profile_id)
    return requests[0].request_id if len(requests) == 1 else None


def _owned_request(profile_id: str, request_id: str | None) -> str | None:
    """The request id only if it exists AND belongs to this profile."""
    if not request_id:
        return None
    request = store.get_request(request_id)
    return request_id if request and request.profile_id == profile_id else None


def active_request_id() -> str | None:
    """
    The request a run should judge against, repairing a stale pointer.

    A run with no request id silently loses the deferred bucket — every parked
    ad would be judged again — so an unbound pointer is fixed here instead of
    being allowed to run blind. Only unambiguous cases are repaired: a pointer
    naming a request of this profile that exists, or a profile with exactly one
    request.
    """
    data = read_active()
    if not data:
        return None
    profile_id = data["profile_id"]
    if bound := _owned_request(profile_id, data.get("request_id")):
        return bound
    if candidate := _owned_request(profile_id, _only_request(profile_id)):
        set_active(profile_id, candidate)
        logger.info("🔗 Linked profile %s to request %s", profile_id, candidate)
        return candidate
    return None


# ── bootstrap ────────────────────────────────────────────────────────────────
async def ensure_active_profile() -> store.Profile:
    """
    The profile a run should use, creating one on first use.

    Idempotent: once the pointer exists this is a file read plus, when the
    profile text has moved on, one classification call.
    """
    existing = active_profile()
    if existing:
        active_request_id()   # repair a pointer written before requests
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

    profile = store.create_profile(about=about_label, profile_text=about)
    request_obj = store.create_request(profile.profile_id, request_text)
    set_active(profile.profile_id, request_obj.request_id)

    logger.info("👨‍👩‍👧 Bootstrapped profile %s (about=%r)", profile.profile_id, profile.about)
    return profile


def _derive_about(profile_text: str) -> str:
    """
    A readable placeholder when the model has not named the profile yet.

    Falls back to the opening line, trimmed on a word boundary. Real naming is
    the classifier's job; this only has to be recognisable in a list.
    """
    first = (profile_text.strip().splitlines() or [""])[0].strip().strip("\"'")
    if not first:
        return "Untitled profile"
    words, out = first.split(), []
    for word in words:
        if sum(len(w) + 1 for w in out) + len(word) > 48:
            break
        out.append(word)
    return " ".join(out) or "Untitled profile"


async def sync_with_profile_file(profile: store.Profile) -> store.Profile:
    """
    Follow about_us.txt if it was edited outside these screens.

    The run pipeline reads the profile files, so if they move the profile has to
    move with them. Structural classification is NOT done here: that is the
    screens' job, with the user present to agree to it.
    """
    text = ABOUT_US_PATH.read_text(encoding="utf-8").strip() if ABOUT_US_PATH.exists() else ""
    if text and text != profile.profile_text:
        profile.profile_text = text
        store.save_profile(profile)
        logger.info("📝 Profile text changed outside the screens — followed for %s", profile.profile_id)
    return profile
