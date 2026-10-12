"""
store.py — persistence for profiles, request profiles and lead lists.

Durability rule, learned the hard way: JSON is the truth, Redis is a mirror.
Redis keys expire and get flushed; the lead list in particular must never lose
a row, so every write goes to disk first and Redis second. A write that
succeeds on only one side is logged loudly rather than silently accepted.

Layout under data/:
  profiles/profiles.json          every Profile + ProfileRequest
  leads/<profile_id>.json          accepted ad records, deduped, never dropped
"""

import json
import logging
from pathlib import Path

from core import Profile, ProfileRequest, run_state_broker, safe_redis_operation

from genesis.config import PROFILES_DIR, LEADS_DIR

logger = logging.getLogger(__name__)

PROFILES_FILE = PROFILES_DIR / "profiles.json"

PROFILE_KEY = "tesserae:profiles:{profile_id}"
LEADS_KEY = "tesserae:leads:{profile_id}"
LEADS_INDEX_KEY = "tesserae:leads:index"

# Lead lists are permanent by design — the user has seen these. The TTL is only
# a safety net against unbounded growth if a profile is deleted.
LEADS_TTL_SECONDS = 365 * 24 * 3600


# ── helpers ──────────────────────────────────────────────────────────────────
def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def new_profile_id(about: str) -> str:
    """Stable-ish, readable, and unique. Slug + short suffix."""
    import uuid

    slug = "-".join(
        ch for ch in "".join(c.lower() if c.isalnum() else " " for c in about).split() if ch
    )[:40] or "profile"
    return f"fam_{slug}_{uuid.uuid4().hex[:6]}"


def new_request_id(profile_id: str) -> str:
    import uuid

    return f"req_{profile_id.removeprefix('fam_')}_{uuid.uuid4().hex[:6]}"


def _read_all() -> dict:
    if not PROFILES_FILE.exists():
        return {"profiles": {}, "requests": {}}
    try:
        data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("❌ profiles.json unreadable (%s) — starting empty", exc)
        return {"profiles": {}, "requests": {}}
    data.setdefault("profiles", {})
    data.setdefault("requests", {})
    return data


def _write_all(data: dict) -> None:
    PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILES_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── profiles ─────────────────────────────────────────────────────────────────
def list_profiles() -> list[Profile]:
    data = _read_all()
    return [Profile(**raw) for raw in data["profiles"].values()]


def get_profile(profile_id: str) -> Profile | None:
    raw = _read_all()["profiles"].get(profile_id)
    return Profile(**raw) if raw else None




def save_profile(profile: Profile) -> Profile:
    profile.updated_at = _now()
    if not profile.created_at:
        profile.created_at = profile.updated_at
    if not profile.founding_text:
        profile.founding_text = profile.profile_text
    data = _read_all()
    data["profiles"][profile.profile_id] = profile.model_dump(mode="json")
    _write_all(data)                      # disk first — the truth
    safe_redis_operation(                                  # mirror second
        run_state_broker.set, PROFILE_KEY.format(profile_id=profile.profile_id),
        json.dumps(profile.model_dump(mode="json"), ensure_ascii=False),
    )
    return profile


def create_profile(about: str, profile_text: str) -> Profile:
    return save_profile(
        Profile(
            profile_id=new_profile_id(about),
            about=about,
            profile_text=profile_text,
            founding_text=profile_text,
        )
    )


# ── request profiles ─────────────────────────────────────────────────────────
def list_requests(profile_id: str | None = None) -> list[ProfileRequest]:
    data = _read_all()["requests"]
    out = [ProfileRequest(**raw) for raw in data.values()]
    return [r for r in out if profile_id is None or r.profile_id == profile_id]


def get_request(request_id: str) -> ProfileRequest | None:
    raw = _read_all()["requests"].get(request_id)
    return ProfileRequest(**raw) if raw else None


def save_request(request_obj: ProfileRequest) -> ProfileRequest:
    request_obj.updated_at = _now()
    if not request_obj.created_at:
        request_obj.created_at = request_obj.updated_at
    if not request_obj.founding_text:
        request_obj.founding_text = request_obj.request_text
    data = _read_all()
    data["requests"][request_obj.request_id] = request_obj.model_dump(mode="json")
    _write_all(data)
    return request_obj


def create_request(profile_id: str, request_text: str) -> ProfileRequest:
    return save_request(
        ProfileRequest(
            request_id=new_request_id(profile_id),
            profile_id=profile_id,
            request_text=request_text,
            founding_text=request_text,
        )
    )


# ── lead lists ───────────────────────────────────────────────────────────────
def leads_path(profile_id: str) -> Path:
    return LEADS_DIR / f"{profile_id}.json"


def load_leads(profile_id: str) -> list[dict]:
    """The user's leads for one profile. Never pruned, never duplicated."""
    path = leads_path(profile_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("❌ lead list %s unreadable (%s)", path, exc)
        return []
    return data if isinstance(data, list) else []


def add_leads(profile_id: str, ads: list[dict]) -> int:
    """
    Append leads, deduped by ad_archive_id. Returns how many were new.

    Disk first: this is the one store the user was told can never lose a row.
    """
    existing = load_leads(profile_id)
    seen = {a.get("ad_archive_id") for a in existing}
    added = 0
    for ad in ads:
        ad_id = ad.get("ad_archive_id")
        if not ad_id or ad_id in seen:
            continue
        seen.add(ad_id)
        existing.append(ad)
        added += 1
    if not added:
        return 0
    leads_path(profile_id).parent.mkdir(parents=True, exist_ok=True)
    leads_path(profile_id).write_text(
        json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    safe_redis_operation(                     # mirror for fast membership checks
        run_state_broker.sadd,
        LEADS_KEY.format(profile_id=profile_id),
        *[a["ad_archive_id"] for a in existing],
    )
    safe_redis_operation(
        run_state_broker.expire, LEADS_KEY.format(profile_id=profile_id), LEADS_TTL_SECONDS
    )
    safe_redis_operation(run_state_broker.sadd, LEADS_INDEX_KEY, profile_id)
    return added


def lead_ids(profile_id: str) -> set[str]:
    """Membership from Redis, falling back to disk if the mirror is cold."""
    ids = safe_redis_operation(
        run_state_broker.smembers, LEADS_KEY.format(profile_id=profile_id)
    )
    if ids:
        return set(ids)
    return {a["ad_archive_id"] for a in load_leads(profile_id) if a.get("ad_archive_id")}


