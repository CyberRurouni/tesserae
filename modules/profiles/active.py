"""
active.py — which profile a run belongs to, and under which request.

A run needs to name exactly one profile and one request: rejections and
deferrals are profile-scoped, so binding a run to nothing means re-judging
ads the user already parked. Rather than guessing "the newest profile", the
project carries an explicit pointer — a guess would silently rebind someone's
verdicts to the wrong profile after an edit.

Bootstrap, when no profile exists yet: create an empty one with an empty
request and point at it. There is deliberately nothing to read it from —
this module used to build the first profile out of about_us.txt, and that
file was a mirror of the store rather than a source, so "first run" and
"hand-edited the file" were indistinguishable. The About You screen is the
only place a profile is written now.

Self-heal: a pointer written before requests existed has no request id. That
is repaired in place rather than rejected, because the profile has exactly one
request and the answer is not ambiguous.
"""

import json
import logging
from pathlib import Path

from genesis.config import PROFILES_DIR

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


def request_for_profile(profile_id: str) -> str | None:
    """
    The request a run under THIS profile should judge against.

    Prefers the active pointer's request id, but only when that request really
    belongs to this profile — a pointer naming another profile's request would
    scope this run's accept-vs-defer decisions to someone else's wishes.
    Falls back to the profile's sole request when it has exactly one, which is
    the unambiguous case, and to None when the answer would be a guess.
    """
    data = read_active() or {}
    if data.get("profile_id") == profile_id:
        if bound := _owned_request(profile_id, data.get("request_id")):
            return bound
    return _owned_request(profile_id, _only_request(profile_id))


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
    The profile a run should use, creating an empty one on first use.

    Idempotent and cheap: once the pointer exists this is a file read. It used
    to also compare the profile against about_us.txt and, if they differed,
    overwrite the stored profile — which quietly made a leftover text file the
    authority over profiles.json. Nothing writes here any more, so there is
    nothing to compare against.
    """
    existing = active_profile()
    if existing:
        active_request_id()   # repair a pointer written before requests
        return existing

    profile = store.create_profile(about="Untitled profile", profile_text="")
    request_obj = store.create_request(profile.profile_id, "")
    set_active(profile.profile_id, request_obj.request_id)

    logger.warning(
        "👤 No profile exists yet — created an empty one (%s). "
        "Write your profile on the 'About you' page before searching, or every "
        "run will judge ads against nothing.",
        profile.profile_id,
    )
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
