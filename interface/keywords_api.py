"""
keywords_api.py — let the user see and choose the harvest set.

Harvest mode is the one part of the run the user genuinely has a say in. It
re-searches keywords that have already been searched, purely to catch ads
posted since — so *which* keywords get that second pass is a judgement about
where new opportunities are most likely to appear, and the person running the
search is better placed to make it than the pipeline is.

Two ways to express it, both supported here:

  - a NUMBER ("harvest the top 10"). The backend ranks by accepted leads and
    takes that many. Ranking is not "most recent" — a keyword that found leads
    is the one most likely to find more, which is the entire premise of a
    second pass.
  - a LIST of keywords. An explicit choice beats a number, and a keyword that
    is not currently searchable is refused with a reason rather than searched
    behind the user's back, because searching it would defeat the cooldown they
    are trying to respect.

Everything is scoped to the active profile's own pool, so one person's keyword
history is never offered to another's run.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from modules.orchestrator import collection as coll

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/keywords", tags=["keywords"])

CATEGORY = "career_jobs"

# Matches RunConfig.harvest_limit. Offered as a shortcut so the common case is
# one click; the field itself accepts any non-negative number.
QUICK_LIMITS = (5, 10, 20, 50)


class HarvestPreviewRequest(BaseModel):
    limit: int = Field(default=20, ge=0)
    keywords: list[str] | None = None


def _pool(category: str = CATEGORY):
    from modules.profiles.active import read_active

    active = read_active() or {}
    return coll.load_collection(category, active.get("profile_id")), active.get("profile_id")


def _row(stats, position: int | None = None) -> dict:
    return {
        "keyword": stats.keyword,
        "ads_fetched": stats.ads_fetched,
        "relevant_ads": stats.relevant_ads,
        "runs_searched": stats.runs_searched,
        "status_hint": str(stats.status_hint),
        "last_searched_at": stats.last_searched_at.isoformat() if stats.last_searched_at else None,
        "reuse_at": stats.reuse_at.isoformat() if stats.reuse_at else None,
        "rank": position,
    }


@router.get("")
async def list_keywords():
    """
    The whole pool for the active profile, ranked, with the default harvest set
    already chosen.

    Returned even when harvest is inactive — the Search page uses it to explain
    that harvesting only applies once the reviewer decides coverage is complete,
    rather than silently hiding a setting that will matter later.
    """
    from modules.orchestrator import collection as _coll

    live, profile_id = _pool()
    searchable = live["searchable"]
    ranked = _coll.rank_for_harvest(searchable)
    default_set, notes = _coll.resolve_harvest(searchable, limit=20)

    return {
        "profile_id": profile_id,
        "category": CATEGORY,
        "harvest_active": _coll.harvest_active(CATEGORY, profile_id),
        "known": len(live["keywords"]),
        "searchable": len(searchable),
        "quick_limits": list(QUICK_LIMITS),
        "default_limit": 20,
        "default_harvest": default_set,
        "notes": notes,
        # Ranked best-first: the UI shows this order, so the top N the backend
        # would pick are the top N the user sees selected.
        "items": [_row(s, i + 1) for i, s in enumerate(ranked)],
        "cooling_down": [_row(s) for s in live["keywords"]
                         if s not in searchable],
    }


@router.post("/preview")
async def preview(payload: HarvestPreviewRequest):
    """
    What this selection would actually re-search, before the run starts.

    The page calls it whenever the user changes the number or the checkboxes,
    so "harvest top 20" is never a guess about which twenty.
    """
    live, _ = _pool()
    chosen, notes = coll.resolve_harvest(
        live["searchable"], limit=payload.limit, chosen=payload.keywords
    )
    return {"keywords": chosen, "count": len(chosen), "notes": notes}


@router.post("/resolve")
async def resolve(payload: HarvestPreviewRequest):
    """
    Resolve a selection to actual keyword names.

    Kept separate from `preview` so the run-start path and the UI's preview use
    exactly the same resolution — the page cannot show one set and the
    orchestrator search another.
    """
    live, _ = _pool()
    chosen, notes = coll.resolve_harvest(
        live["searchable"], limit=payload.limit, chosen=payload.keywords
    )
    if not chosen and not notes:
        raise HTTPException(400, "That selection matches nothing in the searchable pool.")
    logger.info("🧺 Harvest selection resolved: %d keyword(s)", len(chosen))
    return {"keywords": chosen, "notes": notes}