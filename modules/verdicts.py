"""
verdicts.py — the four sets that decide whether an ad is looked at again.

  accepted  {category}          GLOBAL   you already have this lead
  junk      {category}          GLOBAL   nobody is hiring anyone here
  rejected  {profile_id}        PER PROFILE   an employer, just not for you
  deferred  {profile_id}:{request_id}  PER REQUEST   right field, wrong moment

Two of these are global because the answers are objective. An ad either employs
someone or it does not — that does not depend on whose profile is running. An
ad that DOES employ someone is rejected only against the profile that declined
it, because a Node.js role is junk to a Python profile and a lead to a
Node.js one.

The check runs once per scraped card, so every answer is served from an
in-process mirror loaded once per run; Redis is consulted only to fill a mirror
and on write. JSON mirrors every set so a Redis flush costs nothing.
"""

import json
import logging
from pathlib import Path

from genesis.config import DATA_DIR

from core import run_state_broker, safe_redis_operation

logger = logging.getLogger(__name__)

ACCEPTED_KEY = "tesserae:accepted:{category}"
JUNK_KEY = "tesserae:junk:{category}"
REJECTED_KEY = "tesserae:rejected:{profile_id}"

SETS_DIR = DATA_DIR / "sets"

# Global sets only grow — an accepted lead and a piece of junk stay that way.
# These are growth guards, not expiry policies.
SET_TTL_SECONDS = 365 * 24 * 3600


# ── JSON mirrors ─────────────────────────────────────────────────────────────
def _mirror_path(name: str) -> Path:
    return SETS_DIR / f"{name}.json"


def _load_mirror(name: str) -> list[str]:
    path = _mirror_path(name)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("❌ set mirror %s unreadable (%s)", path, exc)
        return []
    return data if isinstance(data, list) else []


def _save_mirror(name: str, ids) -> None:
    SETS_DIR.mkdir(parents=True, exist_ok=True)
    _mirror_path(name).write_text(
        json.dumps(sorted(set(ids)), indent=2), encoding="utf-8"
    )


def _members(key: str) -> set[str]:
    return set(safe_redis_operation(run_state_broker.smembers, key) or [])


def _add(key: str, name: str, ids) -> int:
    """Write Redis then the JSON mirror, and report how many were new."""
    ids = [i for i in {i for i in ids if i} if i not in _load_mirror(name)]
    if not ids:
        return 0
    combined = sorted(set(_load_mirror(name)) | set(ids))
    _save_mirror(name, combined)
    safe_redis_operation(run_state_broker.sadd, key, *ids)
    safe_redis_operation(run_state_broker.expire, key, SET_TTL_SECONDS)
    return len(ids)


def _remove(key: str, name: str, ids) -> int:
    """
    Drop ids from a set, in both mirrors. Returns how many were actually there.

    Needed because the sets are only writable by the judge today, and a user
    correcting a wrong verdict is exactly the case where "final" has to yield.
    JSON is rewritten from the surviving ids rather than patched, so a manual
    edit can never leave a stale id behind.
    """
    doomed = {i for i in ids if i}
    if not doomed:
        return 0
    before = set(_load_mirror(name)) | _members(key)
    survivors = sorted(before - doomed)
    _save_mirror(name, survivors)
    safe_redis_operation(run_state_broker.srem, key, *sorted(doomed & before))
    return len(doomed & before)


# ── global sets ──────────────────────────────────────────────────────────────
def accepted_ids(category: str) -> set[str]:
    return set(_load_mirror(f"accepted_{category}")) | _members(
        ACCEPTED_KEY.format(category=category)
    )


def junk_ids(category: str) -> set[str]:
    return set(_load_mirror(f"junk_{category}")) | _members(
        JUNK_KEY.format(category=category)
    )


def record_accepted(category: str, ad_ids) -> int:
    return _add(ACCEPTED_KEY.format(category=category), f"accepted_{category}", ad_ids)


def record_junk(category: str, ad_ids) -> int:
    return _add(JUNK_KEY.format(category=category), f"junk_{category}", ad_ids)


def is_accepted(category: str, ad_id: str) -> bool:
    return ad_id in accepted_ids(category)


def is_junk(category: str, ad_id: str) -> bool:
    return ad_id in junk_ids(category)


# ── per-profile set ──────────────────────────────────────────────────────────
def rejected_ids(profile_id: str) -> set[str]:
    return set(_load_mirror(f"rejected_{profile_id}")) | _members(
        REJECTED_KEY.format(profile_id=profile_id)
    )


def record_rejected(profile_id: str, ad_ids) -> int:
    return _add(REJECTED_KEY.format(profile_id=profile_id), f"rejected_{profile_id}", ad_ids)


# ── corrections ──────────────────────────────────────────────────────────────
# A verdict the judge got wrong has to be undoable by the user. Accepted and
# junk are GLOBAL, so correcting one clears it for every profile at once —
# there is no per-profile copy to also clean up.
def forget_accepted(category: str, ad_ids) -> int:
    return _remove(ACCEPTED_KEY.format(category=category), f"accepted_{category}", ad_ids)


def forget_junk(category: str, ad_ids) -> int:
    return _remove(JUNK_KEY.format(category=category), f"junk_{category}", ad_ids)


def forget_rejected(profile_id: str, ad_ids) -> int:
    return _remove(REJECTED_KEY.format(profile_id=profile_id), f"rejected_{profile_id}", ad_ids)


def move_rejected(old_profile_id: str, new_profile_id: str, ad_ids) -> int:
    """
    Re-file rejections under a different profile, without ever widening them.

    Rejections are the one verdict that is scoped to a person, so a migration
    like the placeholder→real-profile remap has to MOVE them. Copy-then-delete
    in that order, so an interrupted move loses nothing: the worst case is the
    ids still sitting under the old profile, where they are inert rather than
    applied to the wrong lens.
    """
    if old_profile_id == new_profile_id:
        return 0
    ids = {i for i in ad_ids if i}
    if not ids:
        return 0
    moving = sorted(ids & rejected_ids(old_profile_id))
    if not moving:
        return 0
    record_rejected(new_profile_id, moving)
    forget_rejected(old_profile_id, moving)
    logger.info(
        "📦 Moved %d rejection(s) from %s to %s", len(moving), old_profile_id, new_profile_id
    )
    return len(moving)


def set_verdict(category: str, profile_id: str | None, ad_id: str, outcome: str) -> None:
    """
    Put one ad in exactly one bucket, clearing it from the others.

    The sets are consulted in priority order, so an id left in two of them
    silently keeps the first one's answer — an ad marked both junk and
    accepted would stay skipped even after the user un-junked it. Assigning
    therefore clears every other bucket rather than only adding.
    """
    ad_id = str(ad_id or "").strip()
    if not ad_id:
        raise ValueError("ad_id is required")

    if outcome not in {"accepted", "junk", "rejected", "none"}:
        raise ValueError(f"unknown outcome {outcome!r}")

    forget_accepted(category, [ad_id])
    forget_junk(category, [ad_id])
    if profile_id:
        forget_rejected(profile_id, [ad_id])

    if outcome == "accepted":
        record_accepted(category, [ad_id])
    elif outcome == "junk":
        record_junk(category, [ad_id])
    elif outcome == "rejected":
        if not profile_id:
            raise ValueError("rejected needs a profile — it is the one scoped verdict")
        record_rejected(profile_id, [ad_id])
    logger.info("✏️  %s → %s (profile=%s)", ad_id, outcome, profile_id or "—")


def is_rejected(profile_id: str, ad_id: str) -> bool:
    return ad_id in rejected_ids(profile_id)


# ── the hot path ─────────────────────────────────────────────────────────────
class FilterIndex:
    """
    Everything already decided, in memory, for one run.

    Built once and then read once per scraped card. `check()` is the single
    decision the scraper makes, in the order that matters most: a lead you
    already have outranks everything, then universal junk, then this profile's
    own rejections, then what this request parked.
    """

    def __init__(self, category: str, profile_id: str | None, request_id: str | None = None):
        self.category = category
        self.profile_id = profile_id
        self.request_id = request_id
        self._accepted = accepted_ids(category)
        self._junk = junk_ids(category)
        self._rejected = rejected_ids(profile_id) if profile_id else set()
        # Deferred is owned by modules.profiles.deferred during the transition;
        # imported lazily so this module stays usable without it.
        self._deferred: dict[str, str] = {}
        if profile_id and request_id:
            try:
                from modules.profiles.deferred import load as _load_deferred

                self._deferred = {
                    r["ad_archive_id"]: r.get("request_id", "")
                    for r in _load_deferred(profile_id)
                    if r.get("ad_archive_id")
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("⚠️ deferred bucket unavailable (%s)", exc)

    def load(self) -> "FilterIndex":
        self._accepted = accepted_ids(self.category)
        self._junk = junk_ids(self.category)
        self._rejected = rejected_ids(self.profile_id) if self.profile_id else set()
        logger.info(
            "🧠 Filter index: %d accepted · %d junk · %d rejected for this profile · %d deferred",
            len(self._accepted), len(self._junk), len(self._rejected), len(self._deferred),
        )
        return self

    def refresh(self) -> None:
        """
        Re-read the sets after a batch of verdicts was written.

        Called once per judged batch rather than per ad: a run's own verdicts
        must be visible to the keywords that come after, and reloading per card
        would undo the point of holding them in memory.
        """
        self.load()

    # ── the decision ──────────────────────────────────────────────────────
    def check(self, ad_id: str) -> tuple[bool, str]:
        """
        Should this card be skipped? Returns (skip?, reason).

        Deferred is the only outcome that can come BACK: the same ad under the
        same request stays parked, but a different request gets to judge it
        again. Everything else is final.
        """
        if ad_id in self._accepted:
            return True, "accepted"
        if ad_id in self._junk:
            return True, "junk"
        if ad_id in self._rejected:
            return True, "rejected"
        deferred_by = self._deferred.get(ad_id)
        if deferred_by is not None:
            if self.request_id and deferred_by == self.request_id:
                return True, "deferred"
            return False, "deferred_under_another_request"
        return False, "new"

    def __contains__(self, ad_id: str) -> bool:
        return self.check(ad_id)[0]

    # ── writes, so a verdict reached mid-run is visible to the rest of it ──
    def note_accepted(self, ad_id: str) -> None:
        self._accepted.add(ad_id)

    def note_junk(self, ad_id: str) -> None:
        self._junk.add(ad_id)

    def note_rejected(self, ad_id: str) -> None:
        if self.profile_id:
            self._rejected.add(ad_id)

    def stats(self) -> dict:
        return {
            "accepted": len(self._accepted),
            "junk": len(self._junk),
            "rejected": len(self._rejected),
            "deferred": len(self._deferred),
        }