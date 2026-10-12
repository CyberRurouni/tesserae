"""
deferred.py — the "not now" bucket.

A DEFERRED ad is inside the profile but not wanted under the current request.
It is explicitly NOT trash:

  - it stays bound to the PROFILE profile, never the request profile, so a new
    request inherits the whole pool without re-scraping
  - under the SAME request profile it is not re-judged at all; a re-encounter
    only renews its TTL, which is what lets a long crawl move past it cheaply
  - under a NEW request profile it becomes eligible again, and is re-judged from
    its stored record — one model call, no browser session
  - it can be promoted to `accepted` or stay `deferred`. It can never become
    `rejected`: it already passed the profile test, so calling it trash would
    contradict the profile boundary

The TTL is a growth guard, not an expiry policy — it is refreshed every time
the ad is met, so an ad the user keeps running into never ages out.

Storage mirrors the rest of the system: Redis for membership and TTL, JSON for
the full ad record so re-judging never needs a re-scrape.
"""

import json
import logging
from pathlib import Path

from core import run_state_broker, safe_redis_operation

from . import store

logger = logging.getLogger(__name__)

DEFERRED_KEY = "tesserae:deferred:{profile_id}:{ad_id}"
DEFERRED_INDEX_KEY = "tesserae:deferred:index:{profile_id}"
# Refreshed on every re-encounter, so this only bounds genuinely abandoned ads.
DEFERRED_TTL_SECONDS = 180 * 24 * 3600


def _key(profile_id: str, ad_id: str) -> str:
    return DEFERRED_KEY.format(profile_id=profile_id, ad_id=ad_id)


def _json_path(profile_id: str) -> Path:
    return store.LEADS_DIR.parent / "deferred" / f"{profile_id}.json"


def defer_ad(
    profile_id: str,
    ad: dict,
    request_id: str,
    reason: str = "",
    ad_category: str = "",
) -> None:
    """
    Put an ad in the not-now bucket, or refresh it if already there.

    Re-encountering an ad under the same request does NOT re-judge it, so this
    is the call that renews its TTL — that is the whole cost of passing over it.
    """
    ad_id = ad.get("ad_archive_id")
    if not ad_id:
        return

    record = {
        "ad_archive_id": ad_id,
        "request_id": request_id,
        "reason": reason or ad.get("relevance_reason", ""),
        "ad_category": ad_category,
        "ad": ad,  # the full record, so re-judging needs no re-scrape
    }
    safe_redis_operation(
        run_state_broker.set, _key(profile_id, ad_id),
        json.dumps(record, ensure_ascii=False), ex=DEFERRED_TTL_SECONDS,
    )
    safe_redis_operation(run_state_broker.sadd, DEFERRED_INDEX_KEY.format(profile_id=profile_id), ad_id)
    safe_redis_operation(
        run_state_broker.expire, DEFERRED_INDEX_KEY.format(profile_id=profile_id), DEFERRED_TTL_SECONDS
    )
    _write_record(profile_id, ad_id, record)


def renew(profile_id: str, ad_id: str) -> bool:
    """
    Extend an existing deferral's TTL. Returns False when it is not deferred.

    Called on re-encounter under the SAME request profile, where re-judging
    would be wasted work.
    """
    key = _key(profile_id, ad_id)
    if not safe_redis_operation(run_state_broker.exists, key):
        return False
    safe_redis_operation(run_state_broker.expire, key, DEFERRED_TTL_SECONDS)
    safe_redis_operation(
        run_state_broker.expire, DEFERRED_INDEX_KEY.format(profile_id=profile_id), DEFERRED_TTL_SECONDS
    )
    return True


def is_deferred(profile_id: str, ad_id: str) -> bool:
    return bool(safe_redis_operation(run_state_broker.exists, _key(profile_id, ad_id)))


def get(profile_id: str, ad_id: str) -> dict | None:
    raw = safe_redis_operation(run_state_broker.get, _key(profile_id, ad_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def deferred_ids(profile_id: str) -> set[str]:
    return set(
        safe_redis_operation(
            run_state_broker.smembers, DEFERRED_INDEX_KEY.format(profile_id=profile_id)
        ) or []
    )


def load(profile_id: str) -> list[dict]:
    """Every deferred record for a profile, from disk."""
    path = _json_path(profile_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("❌ deferred store %s unreadable (%s)", path, exc)
        return []
    return data if isinstance(data, list) else []


def candidates_for_new_request(profile_id: str, request_id: str) -> list[dict]:
    """
    Deferred ads worth re-judging under a new request profile.

    Anything deferred by a DIFFERENT request qualifies. An ad deferred by this
    same request profile is not re-judged — the request has not changed, so
    nothing about it changed.
    """
    out = []
    for record in load(profile_id):
        if record.get("request_id") == request_id:
            continue
        out.append(record)
    logger.info(
        "📋 %d deferred ad(s) eligible for re-judging under %s (of %d total)",
        len(out), request_id, len(load(profile_id)),
    )
    return out


def resolve(profile_id: str, ad_id: str) -> None:
    """Drop an ad from the bucket once it is accepted or goes out of scope."""
    safe_redis_operation(run_state_broker.delete, _key(profile_id, ad_id))
    safe_redis_operation(
        run_state_broker.srem, DEFERRED_INDEX_KEY.format(profile_id=profile_id), ad_id
    )
    path = _json_path(profile_id)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    kept = [r for r in data if r.get("ad_archive_id") != ad_id]
    if len(kept) != len(data):
        path.write_text(json.dumps(kept, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_record(profile_id: str, ad_id: str, record: dict) -> None:
    path = _json_path(profile_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: list[dict] = []
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, list) else []
        except (OSError, json.JSONDecodeError):
            data = []
    data = [r for r in data if r.get("ad_archive_id") != ad_id]
    data.append(record)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")