"""
families_api.py — REST surface for the profile screens.

Split out of interface/main.py because this is a different concern from the run
lifecycle: these endpoints are synchronous, user-driven, and every one of them
has to be safe to call twice. `profile/apply` and `request/apply` take a
DECISION (accept or decline a proposed change) — a decline must leave nothing
behind, which is why classification and application are separate calls.

A structural change no longer has to inherit anything: a new profile simply
starts with empty per-profile sets, while accepted leads and universal junk are
global and carry over by definition. So there is no seeding step here at all.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from genesis.config import ABOUT_US_PATH, ADDITIONAL_FILTERS_PATH

from modules.families import deferred as deferred_store
from modules.families import store
from modules.families.classifier import (
    classify_profile_change,
    classify_request_change,
)
from modules.verdicts import accepted_ids, junk_ids, rejected_ids

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/families", tags=["families"])

CATEGORY = "career_jobs"


# ── payloads ────────────────────────────────────────────────────────────────
class ClassifyProfileRequest(BaseModel):
    text: str
    family_id: str | None = None


class ApplyProfileRequest(BaseModel):
    text: str
    family_id: str | None = None
    accept: bool = True
    about: str | None = None


class ClassifyRequestRequest(BaseModel):
    text: str
    family_id: str
    request_family_id: str | None = None


class ApplyRequestRequest(BaseModel):
    text: str
    family_id: str
    request_family_id: str | None = None
    accept: bool = True


class ActivateRequest(BaseModel):
    family_id: str
    request_family_id: str | None = None


# ── helpers ─────────────────────────────────────────────────────────────────
def _sync_profile_file(profile: store.Family, request_text: str) -> None:
    """
    Mirror the active profile back into the profile files.

    The run pipeline reads about_us.txt / additional_filters.txt, so the files
    must track the active profile. Writing them here is what makes the two
    screens the source of truth rather than a parallel copy.
    """
    ABOUT_US_PATH.parent.mkdir(parents=True, exist_ok=True)
    ABOUT_US_PATH.write_text(profile.profile_text, encoding="utf-8")
    ADDITIONAL_FILTERS_PATH.write_text(request_text, encoding="utf-8")


def _active_request_text(family_id: str) -> str:
    requests = store.list_request_families(family_id)
    return requests[0].request_text if requests else ""


def _summary(family: store.Family) -> dict:
    """
    Per-family counts.

    Global sets are reported once, on the summary of whichever profile you are
    looking at — they are not per-profile facts and repeating them under every
    profile would read as if they were.
    """
    return {
        "family_id": family.family_id,
        "about": family.about,
        "profile_text": family.profile_text,
        "created_at": family.created_at,
        "updated_at": family.updated_at,
        "counts": {
            "rejected": len(rejected_ids(family.family_id)),
            "deferred_stored": len(deferred_store.deferred_ids(family.family_id)),
            "leads": len(store.load_leads(family.family_id)),
        },
        "requests": [
            {
                "request_family_id": r.request_family_id,
                "request_text": r.request_text,
            }
            for r in store.list_request_families(family.family_id)
        ],
    }


# ── reads ───────────────────────────────────────────────────────────────────
@router.get("")
async def list_families():
    from modules.families.active import read_active

    current = read_active() or {}
    return {
        "families": [_summary(f) for f in store.list_families()],
        "active_family_id": current.get("family_id"),
        "active_request_family_id": current.get("request_family_id"),
        "global_counts": {
            "accepted": len(accepted_ids(CATEGORY)),
            "junk": len(junk_ids(CATEGORY)),
        },
    }


@router.get("/{family_id}/leads")
async def get_leads(family_id: str):
    if store.get_family(family_id) is None:
        raise HTTPException(404, "Family not found")
    return {"family_id": family_id, "leads": store.load_leads(family_id)}


@router.get("/{family_id}/deferred")
async def get_deferred(family_id: str, request_family_id: str | None = None):
    """
    The not-now bucket.

    With `request_family_id`, returns only the ads a DIFFERENT request
    deferred — i.e. the ones that would be re-judged if that request were
    activated.
    """
    if store.get_family(family_id) is None:
        raise HTTPException(404, "Family not found")
    records = (
        deferred_store.candidates_for_new_request(family_id, request_family_id)
        if request_family_id
        else deferred_store.load(family_id)
    )
    return {"family_id": family_id, "deferred": records}


@router.post("/activate")
async def activate(payload: ActivateRequest):
    from modules.families.active import read_active, set_active

    family = store.get_family(payload.family_id)
    if family is None:
        raise HTTPException(404, "Family not found")
    current = read_active() or {}
    request_family_id = payload.request_family_id or current.get("request_family_id")
    # Never fall back to "": a profile with no request would blank the file.
    _sync_profile_file(family, _active_request_text(payload.family_id) or request_text_of(request_family_id))
    set_active(payload.family_id, request_family_id)
    return {"status": "activated", "family_id": payload.family_id,
            "request_family_id": request_family_id}


def request_text_of(request_family_id: str | None) -> str:
    if not request_family_id:
        return ""
    existing = store.get_request_family(request_family_id)
    return existing.request_text if existing else ""


# ── profile: classify, then apply ───────────────────────────────────────────
@router.post("/profile/classify")
async def classify_profile(payload: ClassifyProfileRequest):
    """
    Judge an edit WITHOUT saving anything.

    The UI calls this as the user types (debounced) to show "this stays in your
    profile" or "this is a different objective — make a new profile?" before
    they commit to anything.
    """
    current_text = ""
    family = store.get_family(payload.family_id) if payload.family_id else None
    if family:
        current_text = family.profile_text

    verdict = await classify_profile_change(current_text, payload.text)

    # With no profile to compare against there is nothing to widen or narrow,
    # so a brand-new profile is never "a change of objective". Left as-is the
    # classifier compares against empty text and can invent a structural change
    # that does not exist.
    if not family:
        verdict.change = "none"
        verdict.new_family_required = False

    return {
        "change": verdict.change,
        "new_family_required": verdict.new_family_required,
        "about": verdict.about,
        "explanation": verdict.explanation,
    }


@router.post("/profile/apply")
async def apply_profile(payload: ApplyProfileRequest):
    """
    Commit an edit. `accept=false` writes NOTHING — that is the whole point of
    splitting classify from apply.

    A structural change now starts a profile with EMPTY per-profile sets.
    Accepted leads and universal junk are global, so they carry over without
    any copying or inheritance step.
    """
    if not payload.accept:
        return {"status": "declined", "saved": False}

    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "Profile text cannot be empty")

    from modules.families.active import read_active, set_active

    family = store.get_family(payload.family_id) if payload.family_id else None
    current = read_active() or {}

    # ── a brand-new profile ───────────────────────────────────────────────
    if family is None:
        verdict = await classify_profile_change("", text)
        created = store.create_family(
            about=verdict.about or payload.about or "Untitled profile",
            profile_text=text,
        )
        # A profile with no request would leave the request file blank, so give
        # it one from whatever the user currently has.
        request_family = store.create_request_family(
            created.family_id, request_text_of(current.get("request_family_id"))
        )
        set_active(created.family_id, request_family.request_family_id)
        _sync_profile_file(created, request_family.request_text)
        logger.info("🌱 New profile %s created (about=%r)", created.family_id, created.about)
        return {"status": "created", "saved": True, "family": _summary(created),
                "about": created.about}

    verdict = await classify_profile_change(family.profile_text, text)

    # ── cosmetic: edit in place ───────────────────────────────────────────
    if not verdict.new_family_required:
        family.profile_text = text
        if verdict.about:
            family.about = verdict.about
        store.save_family(family)
        if current.get("family_id") == family.family_id:
            _sync_profile_file(family, _active_request_text(family.family_id))
        return {"status": "updated", "saved": True, "family": _summary(family)}

    # ── structural: a new profile with clean per-profile state ─────────────
    created = store.create_family(
        about=verdict.about or family.about, profile_text=text
    )
    request_family = store.create_request_family(
        created.family_id, _active_request_text(family.family_id)
    )
    set_active(created.family_id, request_family.request_family_id)
    _sync_profile_file(created, request_family.request_text)
    logger.info(
        "🌿 %s: new profile %s (%s) — %s",
        verdict.change, created.family_id, created.about, verdict.explanation,
    )
    return {"status": "created", "saved": True, "family": _summary(created),
            "change": verdict.change, "explanation": verdict.explanation}


# ── request: classify, then apply ───────────────────────────────────────────
@router.post("/request/classify")
async def classify_request(payload: ClassifyRequestRequest):
    if store.get_family(payload.family_id) is None:
        raise HTTPException(404, "Family not found")
    current_text = ""
    if payload.request_family_id:
        existing = store.get_request_family(payload.request_family_id)
        current_text = existing.request_text if existing else ""
    verdict = await classify_request_change(current_text, payload.text)
    return {
        "change": verdict.change,
        "new_family_required": verdict.new_family_required,
        "explanation": verdict.explanation,
    }


@router.post("/request/apply")
async def apply_request(payload: ApplyRequestRequest):
    """
    Commit a request edit.

    A structural change starts a new request. That is the whole mechanism
    behind "a deferred ad gets reconsidered": a new request has a new id, so
    nothing is deferred under it, so the bucket comes back for judgement. No
    re-scraping — the ad record is already stored.
    """
    if not payload.accept:
        return {"status": "declined", "saved": False}
    if store.get_family(payload.family_id) is None:
        raise HTTPException(404, "Family not found")

    from modules.families.active import read_active, set_active

    text = payload.text.strip()
    existing = (
        store.get_request_family(payload.request_family_id) if payload.request_family_id else None
    )

    if existing is None:
        created = store.create_request_family(payload.family_id, text)
        _mirror_if_active(payload.family_id, text, set_active, read_active)
        return {"status": "created", "saved": True, "request": created.model_dump(mode="json")}

    verdict = await classify_request_change(existing.request_text, text)
    if not verdict.new_family_required:
        existing.request_text = text
        store.save_request_family(existing)
        _mirror_if_active(payload.family_id, text, set_active, read_active)
        return {"status": "updated", "saved": True,
                "request": existing.model_dump(mode="json"), "change": verdict.change}

    created = store.create_request_family(payload.family_id, text)
    set_active(payload.family_id, created.request_family_id)
    _mirror_if_active(payload.family_id, text, set_active, read_active)
    logger.info(
        "🎯 Request %s: new request %s — %s",
        verdict.change, created.request_family_id, verdict.explanation,
    )
    return {"status": "created", "saved": True,
            "request": created.model_dump(mode="json"),
            "change": verdict.change, "explanation": verdict.explanation}


def _mirror_if_active(family_id, request_text, set_active, read_active) -> None:
    family = store.get_family(family_id)
    if family and (read_active() or {}).get("family_id") == family_id:
        _sync_profile_file(family, request_text)