"""
deferred.py — the "not now" bucket.

A DEFERRED ad is inside the family but not wanted under the current request.
It is explicitly NOT trash:

  - it stays bound to the PROFILE family, never the request family, so a new
    request inherits the whole pool without re-scraping
  - under the SAME request family it is not re-judged at all; a re-encounter
    only renews its TTL, which is what lets a long crawl move past it cheaply
  - under a NEW request family it becomes eligible again, and is re-judged from
    its stored record — one model call, no browser session
  - it can be promoted to `accepted` or stay `deferred`. It can never become
    `rejected`: it already passed the family test, so calling it trash would
    contradict the family boundary

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

DEFERRED_KEY = "tesserae:deferred:{family_id}:{ad_id}"
DEFERRED_INDEX_KEY = "tesserae:deferred:index:{family_id}"
# Refreshed on every re-encounter, so this only bounds genuinely abandoned ads.
DEFERRED_TTL_SECONDS = 180 * 24 * 3600


def _key(family_id: str, ad_id: str) -> str:
    return DEFERRED_KEY.format(family_id=family_id, ad_id=ad_id)


def _json_path(family_id: str) -> Path:
    return store.LEADS_DIR.parent / "deferred" / f"{family_id}.json"


def defer_ad(
    family_id: str,
    ad: dict,
    request_family_id: str,
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
        "request_family_id": request_family_id,
        "reason": reason or ad.get("relevance_reason", ""),
        "ad_category": ad_category,
        "ad": ad,  # the full record, so re-judging needs no re-scrape
    }
    safe_redis_operation(
        run_state_broker.set, _key(family_id, ad_id),
        json.dumps(record, ensure_ascii=False), ex=DEFERRED_TTL_SECONDS,
    )
    safe_redis_operation(run_state_broker.sadd, DEFERRED_INDEX_KEY.format(family_id=family_id), ad_id)
    safe_redis_operation(
        run_state_broker.expire, DEFERRED_INDEX_KEY.format(family_id=family_id), DEFERRED_TTL_SECONDS
    )
    _write_record(family_id, ad_id, record)


def renew(family_id: str, ad_id: str) -> bool:
    """
    Extend an existing deferral's TTL. Returns False when it is not deferred.

    Called on re-encounter under the SAME request family, where re-judging
    would be wasted work.
    """
    key = _key(family_id, ad_id)
    if not safe_redis_operation(run_state_broker.exists, key):
        return False
    safe_redis_operation(run_state_broker.expire, key, DEFERRED_TTL_SECONDS)
    safe_redis_operation(
        run_state_broker.expire, DEFERRED_INDEX_KEY.format(family_id=family_id), DEFERRED_TTL_SECONDS
    )
    return True


def is_deferred(family_id: str, ad_id: str) -> bool:
    return bool(safe_redis_operation(run_state_broker.exists, _key(family_id, ad_id)))


def get(family_id: str, ad_id: str) -> dict | None:
    raw = safe_redis_operation(run_state_broker.get, _key(family_id, ad_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def deferred_ids(family_id: str) -> set[str]:
    return set(
        safe_redis_operation(
            run_state_broker.smembers, DEFERRED_INDEX_KEY.format(family_id=family_id)
        ) or []
    )


def load(family_id: str) -> list[dict]:
    """Every deferred record for a family, from disk."""
    path = _json_path(family_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("❌ deferred store %s unreadable (%s)", path, exc)
        return []
    return data if isinstance(data, list) else []


def candidates_for_new_request(family_id: str, request_family_id: str) -> list[dict]:
    """
    Deferred ads worth re-judging under a new request family.

    Anything deferred by a DIFFERENT request qualifies. An ad deferred by this
    same request family is not re-judged — the request has not changed, so
    nothing about it changed.
    """
    out = []
    for record in load(family_id):
        if record.get("request_family_id") == request_family_id:
            continue
        out.append(record)
    logger.info(
        "📋 %d deferred ad(s) eligible for re-judging under %s (of %d total)",
        len(out), request_family_id, len(load(family_id)),
    )
    return out


def resolve(family_id: str, ad_id: str) -> None:
    """Drop an ad from the bucket once it is accepted or goes out of scope."""
    safe_redis_operation(run_state_broker.delete, _key(family_id, ad_id))
    safe_redis_operation(
        run_state_broker.srem, DEFERRED_INDEX_KEY.format(family_id=family_id), ad_id
    )
    path = _json_path(family_id)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    kept = [r for r in data if r.get("ad_archive_id") != ad_id]
    if len(kept) != len(data):
        path.write_text(json.dumps(kept, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_record(family_id: str, ad_id: str, record: dict) -> None:
    path = _json_path(family_id)
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