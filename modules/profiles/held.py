"""
held.py — accepted ads the judge was not confident enough about.

A HELD ad already passed the profile judge. It is not junk and not "right field,
wrong moment" (that is deferred). It is a lead the user's strictness setting
kept out of the lead list for now:

  - confidence 0.50–threshold        -> "borderline"
  - confidence below 0.50            -> "very_low" (no slider can promote it;
                                        the slider floor is 0.50)

The record carries the judge's confidence and the threshold it missed. Raising a
threshold never demotes an existing lead. Lowering one lets the ad be re-gated
locally: a request-stage held ad can become accepted with no AI call, while a
profile-stage held ad needs one request-judge call.

A profile-stage hold is request-blind: if only the request changes, the profile
answer has not changed. A request-stage hold belongs to one request. That
asymmetry is deliberate and saves re-running the profile judge when the user
edits what they want now.
"""

import json
import logging
from pathlib import Path

from . import store

logger = logging.getLogger(__name__)

# A hold has no TTL: it is a lead waiting on a user setting, not an abandoned
# ad. Keeping it until it is promoted, deferred or rejected is the point.
HELD_STAGE_PROFILE = "profile"
HELD_STAGE_REQUEST = "request"


def _json_path(profile_id: str) -> Path:
    return store.LEADS_DIR.parent / "held" / f"{profile_id}.json"


def load(profile_id: str) -> list[dict]:
    """Every held record for a profile, from disk."""
    if not profile_id:
        return []
    path = _json_path(profile_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("❌ held store %s unreadable (%s)", path, exc)
        return []
    return data if isinstance(data, list) else []


def for_lens(profile_id: str, request_id: str | None) -> list[dict]:
    """
    Held records that apply to one profile + request.

    Profile-stage holds apply to every request; request-stage holds only apply
    to the request that produced them.
    """
    out = []
    for record in load(profile_id):
        stage = record.get("stage")
        if stage == HELD_STAGE_PROFILE:
            out.append(record)
        elif stage == HELD_STAGE_REQUEST and record.get("request_id") == request_id:
            out.append(record)
    return out


def hold_ad(
    profile_id: str,
    ad: dict,
    *,
    stage: str,
    request_id: str | None,
    confidence: float,
    threshold: float,
    reason: str = "",
    ad_category: str = "",
) -> dict:
    """
    Store an accepted ad below the user's threshold, or refresh it.

    One active hold per ad: a re-judgement replaces the previous stage/request
    rather than leaving two contradictory records behind.
    """
    if not profile_id:
        return {}
    ad_id = ad.get("ad_archive_id")
    if not ad_id:
        return {}

    record = {
        "ad_archive_id": ad_id,
        "stage": stage,
        "request_id": request_id if stage == HELD_STAGE_REQUEST else None,
        "confidence": float(confidence),
        "held_threshold": float(threshold),
        "held_kind": "very_low" if confidence < 0.5 else "borderline",
        "reason": reason or ad.get("relevance_reason", ""),
        "ad_category": ad_category or ad.get("ad_category", ""),
        # The full record, so a lowered threshold needs no re-scrape.
        "ad": ad,
    }
    _write_record(profile_id, ad_id, record)
    return record


def resolve(profile_id: str, ad_id: str) -> None:
    """Drop every hold for one ad once it is accepted, deferred or rejected."""
    if not profile_id:
        return
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


def update_threshold(
    profile_id: str, ad_id: str, threshold: float,
    *, stage: str | None = None, request_id: str | None = None,
) -> bool:
    """
    Record that an ad was re-gated and still missed the new threshold.

    Updating rather than deleting matters: the next run should skip it again
    until the threshold moves lower than this new value.
    """
    if not profile_id:
        return False
    path = _json_path(profile_id)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    changed = False
    for record in data:
        if record.get("ad_archive_id") != ad_id:
            continue
        if stage and record.get("stage") != stage:
            continue
        if stage == HELD_STAGE_REQUEST and request_id is not None \
                and record.get("request_id") != request_id:
            continue
        record["held_threshold"] = float(threshold)
        record["held_kind"] = (
            "very_low" if float(record.get("confidence") or 0) < 0.5 else "borderline"
        )
        changed = True
    if changed:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return changed


def skip_for(
    record: dict, profile_threshold: float, request_threshold: float,
) -> bool:
    """
    Should this held ad be skipped under the current thresholds?

    Only when the new threshold is at least the one it already missed. A lower
    threshold means the stored confidence deserves another look.
    """
    threshold = (
        profile_threshold
        if record.get("stage") == HELD_STAGE_PROFILE
        else request_threshold
    )
    return threshold >= float(record.get("held_threshold") or 0.5)


def clears(record: dict, threshold: float) -> bool:
    """Is the stored confidence enough for the lowered threshold?"""
    return float(record.get("confidence") or 0.0) >= float(threshold)


def _write_record(profile_id: str, ad_id: str, record: dict) -> None:
    path = _json_path(profile_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load(profile_id)
    data = [r for r in data if r.get("ad_archive_id") != ad_id]
    data.append(record)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
