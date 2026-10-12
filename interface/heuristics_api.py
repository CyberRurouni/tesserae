"""
heuristics_api.py — manage the user's own first-sieve patterns.

Read-modify-write on one JSON file. Every write returns the whole resulting set
rather than a delta, so the UI can render what actually stuck (a blank or
duplicate pattern is dropped, and the user should see that rather than have
their edit silently not apply).
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.schemas.category import CATEGORIES, DEFAULT_CATEGORY_ID
from modules import ad_archive, heuristics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/heuristics", tags=["heuristics"])

CATEGORY = "career_jobs"


class HeuristicsUpdate(BaseModel):
    require_ad_text_patterns: list[str] = Field(default_factory=list)
    require_any_ad_text_patterns: list[str] = Field(default_factory=list)
    exclude_ad_text_patterns: list[str] = Field(default_factory=list)


def _category_heuristics() -> dict[str, list[str]]:
    category = CATEGORIES.get(CATEGORY, CATEGORIES[DEFAULT_CATEGORY_ID])
    return category.heuristics.model_dump()


@router.get("")
async def read():
    """
    The user's patterns, the category's built-ins, and what each would drop.

    The preview counts come from ad text already on disk, so they are real
    numbers from this user's own history rather than a sample.
    """
    mine = heuristics.load()
    built_in = _category_heuristics()
    combined = heuristics.merged(built_in)

    # Sample real ad text from the archive so the preview counts mean something
    # rather than being illustrative.
    from modules.verdicts import accepted_ids, junk_ids

    texts = []
    sample_ids = sorted(accepted_ids(CATEGORY))[:60] + sorted(junk_ids(CATEGORY))[:60]
    for ad_id in sample_ids:
        record = ad_archive.get_ad(CATEGORY, ad_id)
        if record and record.get("ad_text"):
            texts.append(record["ad_text"])

    return {
        "user": mine,
        "builtin": built_in,
        "combined": combined,
        "labels": heuristics.LABELS,
        "preview": heuristics.preview(mine, texts),
        "sample_size": len(texts),
        "note": (
            "Your patterns are added to the category's built-ins, never replacing them. "
            "“Must contain all of” is left to the category on purpose — stacking "
            "requirements narrows the sieve until it finds nothing."
        ),
    }


@router.put("")
async def update(payload: HeuristicsUpdate):
    """Replace the user's patterns wholesale and return what was stored."""
    saved = heuristics.save(payload.model_dump())
    return {"status": "ok", "user": saved, "combined": heuristics.merged(_category_heuristics())}
