"""
verdicts_api.py — inspect and correct the four sets.

The sets exist so the scraper can answer "has this been decided?" in O(1). This
module is the other half: the user can look at what was decided and change it.

Two rules the endpoints keep:

  1. accepted and junk are GLOBAL. Correcting one clears it for every profile at
     once, and the response says so — a user un-junking an ad should not have
     to wonder whether other profiles were affected. They were.
  2. rejected is the only profile-scoped verdict, so it is the only one that
     takes a profile_id, and assigning it without one is an error rather than a
     silent no-op.

Deleting is supported on purpose. These sets were described as final, and a
final verdict the user disagrees with has to be undoable, or the classification
becomes something they can only live with.
"""

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from modules import ad_archive
from modules.verdicts import (
    accepted_ids,
    forget_accepted,
    forget_junk,
    forget_rejected,
    junk_ids,
    record_rejected,
    rejected_ids,
    set_verdict,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/verdicts", tags=["verdicts"])

CATEGORY = "career_jobs"

OUTCOMES = ("accepted", "junk", "rejected", "none")

# Page size is a cap, not a default: 115 ids is the whole junk set today, but
# the accepted set grows every time a run finds a lead.
MAX_LIMIT = 500


class VerdictRequest(BaseModel):
    ad_id: str = Field(min_length=1)
    outcome: str = Field(pattern="^(accepted|junk|rejected|none)$")
    profile_id: str | None = None


class BulkRequest(BaseModel):
    ad_ids: list[str] = Field(min_length=1)
    outcome: str = Field(pattern="^(accepted|junk|rejected|none)$")
    profile_id: str | None = None


def _require_profile(profile_id: str | None) -> str:
    if not profile_id:
        raise HTTPException(
            400,
            "rejected needs a profile_id — it is the only profile-scoped verdict",
        )
    return profile_id


def _ids_for(outcome: str, profile_id: str | None) -> set[str]:
    if outcome == "accepted":
        return accepted_ids(CATEGORY)
    if outcome == "junk":
        return junk_ids(CATEGORY)
    return rejected_ids(_require_profile(profile_id))


@router.get("")
async def list_verdicts(
    outcome: str = Query(..., pattern="^(accepted|junk|rejected)$"),
    profile_id: str | None = None,
    search: str | None = None,
    limit: int = Query(100, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    """
    One page of a set, newest run files first.

    `search` filters on advertiser name and ad text, which is how you find the
    one ad you remember out of 89 pieces of junk.
    """
    ids = sorted(_ids_for(outcome, profile_id))
    total = len(ids)

    if search:
        needle = search.strip().lower()
        matched = []
        for ad_id in ids:
            row = ad_archive.summarize(
                ad_archive.get_ad(CATEGORY, ad_id), ad_id, outcome
            )
            haystack = f"{row['advertiser_name']} {row['ad_text']}".lower()
            if needle in haystack:
                matched.append(row)
        rows = matched
    else:
        rows = [
            ad_archive.summarize(ad_archive.get_ad(CATEGORY, ad_id), ad_id, outcome)
            for ad_id in ids
        ]

    page = rows[offset : offset + limit]
    return {
        "outcome": outcome,
        "profile_id": profile_id if outcome == "rejected" else None,
        "total": total,
        "matched": len(rows),
        "offset": offset,
        "limit": limit,
        "scope": "global" if outcome in ("accepted", "junk") else "profile",
        "items": page,
    }


@router.get("/counts")
async def counts(profile_id: str | None = None):
    """Sizes of every set, so the UI can show where the bulk actually is."""
    return {
        "accepted": len(accepted_ids(CATEGORY)),
        "junk": len(junk_ids(CATEGORY)),
        "rejected": len(rejected_ids(profile_id)) if profile_id else 0,
    }


@router.post("")
async def set_verdict_endpoint(payload: VerdictRequest):
    """
    Move one ad into exactly one bucket.

    Clears the ad from the other sets first — the filter checks in priority
    order, so an id left in two sets would keep the first answer and appear to
    ignore the edit.
    """
    try:
        set_verdict(
            CATEGORY,
            _require_profile(payload.profile_id) if payload.outcome == "rejected"
            else payload.profile_id,
            payload.ad_id,
            payload.outcome,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return {
        "status": "ok",
        "ad_id": payload.ad_id,
        "outcome": payload.outcome,
        "global": payload.outcome in ("accepted", "junk"),
        "note": (
            "Accepted and junk are global — this applies to every profile."
            if payload.outcome in ("accepted", "junk")
            else "Rejections apply to this profile only."
            if payload.outcome == "rejected"
            else "Cleared from every set."
        ),
    }


@router.post("/bulk")
async def bulk_set(payload: BulkRequest):
    """
    Apply one verdict to many ads — the "this whole page is junk" action.

    Mixed outcomes in one call are rejected rather than partially applied: the
    user meant one thing by the button they pressed.
    """
    if payload.outcome == "rejected":
        _require_profile(payload.profile_id)

    targets = [i for i in dict.fromkeys(a.strip() for a in payload.ad_ids) if i]
    if not targets:
        raise HTTPException(400, "no ad ids given")

    changed = 0
    if payload.outcome == "none":
        changed = (
            forget_accepted(CATEGORY, targets)
            + forget_junk(CATEGORY, targets)
            + (forget_rejected(payload.profile_id, targets) if payload.profile_id else 0)
        )
    else:
        for ad_id in targets:
            try:
                set_verdict(CATEGORY, payload.profile_id, ad_id, payload.outcome)
                changed += 1
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

    return {"status": "ok", "outcome": payload.outcome, "changed": changed}


@router.post("/restore-accepted")
async def restore_accepted(payload: BulkRequest):
    """
    Un-junk ads the user did not mean to kill.

    The closest thing to a restore that exists: junk is the set that grows
    without limit and the one whose mistakes are invisible until you notice an
    ad you wanted is gone.
    """
    targets = [i for i in dict.fromkeys(a.strip() for a in payload.ad_ids) if i]
    if not targets:
        raise HTTPException(400, "no ad ids given")
    restored = 0
    for ad_id in targets:
        set_verdict(CATEGORY, payload.profile_id, ad_id, "accepted")
        restored += 1
    logger.info("♻️  Restored %d ad(s) to accepted", restored)
    return {"status": "ok", "restored": restored}
