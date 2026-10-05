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

from . import store, taxonomy, verdicts

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

    # Classify BEFORE creating, so the family's id and slug come from the name
    # the model gives it rather than a raw first line of the profile.
    verdict = None
    try:
        from .classifier import classify_profile_change

        verdict = await classify_profile_change("", about)
    except Exception as exc:  # noqa: BLE001 - bootstrap must survive a model outage
        logger.warning("⚠️ Could not classify family (%s) — naming it from the profile", exc)

    family = store.create_family(
        # The name is worth keeping even when the scope is rejected — it is
        # independent of the taxonomy and reads far better than the derived
        # first line of the profile.
        about=(verdict.about if verdict and verdict.about else "") or _derive_about(about),
        category_domain=verdict.category_domain if verdict else "",
        specialisations=verdict.specialisations if verdict else [],
        profile_text=about,
    )
    request_family = store.create_request_family(family.family_id, request_text)
    set_active(family.family_id, request_family.request_family_id)

    if family.category_domain:
        logger.info(
            "🧭 Placed family in taxonomy: %s / %s (about=%r)",
            family.category_domain, family.specialisations, family.about,
        )
    else:
        logger.info("🧭 Named family %r (scope left unclassified)", family.about)

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
    Keep the family in step with about_us.txt, classifying the difference.

    The config UI still edits the profile FILE directly, so without this the
    family would drift silently and every verdict would be filed under a lens
    the user no longer holds.

    Temporary bridge: a structural change is applied automatically and the
    reason is logged, because there is no confirmation UI yet. Once the family
    screens exist this should propose the change and wait instead — the log
    line is the only record of the decision today.
    """
    from .classifier import classify_profile_change
    from .policies import seed_plan_for_edit

    about = ABOUT_US_PATH.read_text(encoding="utf-8").strip() if ABOUT_US_PATH.exists() else ""
    if not about or about == family.profile_text:
        return family

    logger.info("📝 Profile text changed since this family was written — classifying the edit")
    try:
        verdict = await classify_profile_change(family.profile_text, about)
    except Exception as exc:  # noqa: BLE001 - never block a run on a model outage
        logger.warning(
            "⚠️ Could not classify the profile edit (%s) — keeping the current family as-is", exc
        )
        return family

    if not verdict.new_family_required:
        family.profile_text = about
        if verdict.about:
            family.about = verdict.about
        if verdict.category_domain and verdict.specialisations:
            family.category_domain = verdict.category_domain
            family.specialisations = verdict.specialisations
        store.save_family(family)
        logger.info("✅ Cosmetic profile edit — applied to %s in place", family.family_id)
        return family

    # A structural change. Create the node and seed it, then adopt it.
    plan = seed_plan_for_edit(verdict.change, family_id=family.family_id)
    child = store.create_family(
        about=verdict.about or family.about,
        category_domain=verdict.category_domain or family.category_domain,
        specialisations=verdict.specialisations or family.specialisations,
        profile_text=about,
        seeded_from=[family.family_id],
    )
    child.generation = family.generation + 1
    store.save_family(child)
    set_active(child.family_id, active.read_active().get("request_family_id"))

    logger.warning(
        "🌿 %s -> new family %s (%s): %s\n    Seeding: %s\n"
        "    TODO: this was applied WITHOUT confirmation. The family screens will "
        "propose the change and wait for a decision.",
        verdict.change, child.family_id, child.about, verdict.explanation, plan.summary(),
    )
    return child


# ── one-time migration off the profile-text hash ──────────────────────────────
def legacy_namespace(category: str) -> tuple[str, list[str]]:
    """
    Find the legacy hash namespace that matches the CURRENT profile text, and
    return it with its keys. Returns ("", []) when there is nothing to migrate.
    """
    from redis import Redis

    from genesis.config import REDIS_HOST, REDIS_PORT
    from core.utils.redis_utils import create_redis_connection

    about = ABOUT_US_PATH.read_text(encoding="utf-8").strip() if ABOUT_US_PATH.exists() else ""
    request_text = (
        ADDITIONAL_FILTERS_PATH.read_text(encoding="utf-8").strip()
        if ADDITIONAL_FILTERS_PATH.exists()
        else ""
    )
    import hashlib

    about_hash = hashlib.sha256(about.encode("utf-8")).hexdigest()[:16]
    filter_hash = hashlib.sha256(request_text.encode("utf-8")).hexdigest()[:16]
    pattern = f"{LEGACY_SEEN_PREFIX}:{category}:{about_hash}:{filter_hash}:*"
    try:
        conn = create_redis_connection(host=REDIS_HOST, port=REDIS_PORT, db=1)
        ids = [k.rsplit(":", 1)[-1] for k in conn.scan_iter(match=pattern, count=500)]
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Legacy namespace scan failed (%s)", exc)
        return "", []
    return pattern, ids


def _accepted_ids(category: str) -> set[str]:
    """Ids we know were accepted, recovered from the append-only store."""
    from genesis.config import ADS_DIR

    path = ADS_DIR / category / "stored_ads.json"
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {a["ad_archive_id"] for a in data if isinstance(a, dict) and a.get("ad_archive_id")}


def migrate_legacy_verdicts(category: str, family_id: str) -> dict[str, int]:
    """
    Move the legacy hash namespace into a family's verdict store.

    Idempotent — re-running just rewrites the same keys.
    """
    pattern, ids = legacy_namespace(category)
    if not ids:
        logger.info("ℹ️ No legacy verdicts to migrate for %s", category)
        return {"accepted": 0, "rejected": 0}

    accepted = _accepted_ids(category)
    counts = {"accepted": 0, "rejected": 0}
    for ad_id in ids:
        outcome = "accepted" if ad_id in accepted else "rejected"
        verdicts.record_verdict(
            category, family_id, ad_id, outcome,
            reason="migrated from the pre-family verdict namespace",
        )
        counts[outcome] += 1
    logger.info(
        "📦 Migrated %d legacy verdict(s) into %s — %d accepted, %d rejected",
        len(ids), family_id, counts["accepted"], counts["rejected"],
    )
    return counts
