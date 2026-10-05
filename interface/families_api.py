"""
families_api.py — REST surface for the family screens.

Split out of interface/main.py because this is a different concern from the run
lifecycle: these endpoints are synchronous, user-driven, and every one of them
has to be safe to call twice. In particular `profile/apply` and
`request/apply` take a DECISION (accept or decline a proposed node) — a
decline must leave nothing behind, which is why classification and application
are separate calls.
"""

import json
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from genesis.config import ABOUT_US_PATH, ADDITIONAL_FILTERS_PATH

from modules.families import active as active_mod
from modules.families import deferred as deferred_store
from modules.families import store, taxonomy, verdicts
from modules.families.classifier import (
    check_scope,
    classify_profile_change,
    classify_request_change,
)
from modules.families.policies import (
    plan_for_new_family,
    plan_for_narrowing,
    plan_for_widening,
)

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
    # Only used when creating a node the classifier could not place.
    about: str | None = None
    category_domain: str | None = None
    specialisations: list[str] = Field(default_factory=list)


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
def _sync_profile_file(family: store.Family, request_text: str) -> None:
    """
    Mirror the active family back into the profile files.

    The run pipeline reads about_us.txt / additional_filters.txt, so the files
    must track the active family. Writing them here is what makes the two-page
    screens the source of truth rather than a parallel copy.
    """
    ABOUT_US_PATH.parent.mkdir(parents=True, exist_ok=True)
    ABOUT_US_PATH.write_text(family.profile_text, encoding="utf-8")
    ADDITIONAL_FILTERS_PATH.write_text(request_text, encoding="utf-8")


def _active_request_text(family_id: str) -> str:
    requests = store.list_request_families(family_id)
    return requests[0].request_text if requests else ""


def _summary(family: store.Family) -> dict:
    counts = verdicts.stats(CATEGORY, family.family_id)
    return {
        "family_id": family.family_id,
        "about": family.about,
        "category_domain": family.category_domain,
        "specialisations": family.specialisations,
        "profile_text": family.profile_text,
        "generation": family.generation,
        "seeded_from": family.seeded_from,
        "ancestors": store.ancestry(family.family_id),
        "created_at": family.created_at,
        "updated_at": family.updated_at,
        "counts": {
            **counts,
            "leads": len(store.load_leads(family.family_id)),
            "deferred_stored": len(deferred_store.deferred_ids(family.family_id)),
        },
        "requests": [
            {
                "request_family_id": r.request_family_id,
                "request_text": r.request_text,
                "generation": r.generation,
                "seeded_from": r.seeded_from,
            }
            for r in store.list_request_families(family.family_id)
        ],
    }


# ── reads ───────────────────────────────────────────────────────────────────
@router.get("")
async def list_families():
    """Every family, with the active one flagged so the UI can preselect it."""
    current = active_mod.read_active() or {}
    return {
        "families": [_summary(f) for f in store.list_families()],
        "active_family_id": current.get("family_id"),
        "active_request_family_id": current.get("request_family_id"),
    }


@router.get("/taxonomy")
async def get_taxonomy():
    """The closed vocabulary, so the UI can show where a family sits."""
    return {
        "domains": taxonomy.SPECIALISATIONS,
        "chains": taxonomy.all_chains(),
        "focuses": taxonomy.FOCUSES,
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
    if request_family_id:
        records = deferred_store.candidates_for_new_request(family_id, request_family_id)
    else:
        records = deferred_store.load(family_id)
    return {"family_id": family_id, "deferred": records}


@router.post("/activate")
async def activate(payload: ActivateRequest):
    family = store.get_family(payload.family_id)
    if family is None:
        raise HTTPException(404, "Family not found")
    current = active_mod.read_active() or {}
    request_family_id = payload.request_family_id or current.get("request_family_id")
    active_mod.set_active(payload.family_id, request_family_id)
    _sync_profile_file(family, _active_request_text(payload.family_id))
    return {"status": "activated", "family_id": payload.family_id,
            "request_family_id": request_family_id}


# ── profile: classify, then apply ───────────────────────────────────────────
@router.post("/profile/classify")
async def classify_profile(payload: ClassifyProfileRequest):
    """
    Judge an edit WITHOUT saving anything.

    The UI calls this as the user types (debounced) to show "this belongs to
    your Python family" or "this is a new objective — create a new profile?"
    before they commit to anything.
    """
    current_text = ""
    family = store.get_family(payload.family_id) if payload.family_id else None
    if family:
        current_text = family.profile_text

    verdict = await classify_profile_change(current_text, payload.text)
    if verdict.category_domain and not taxonomy.is_valid(
        verdict.category_domain, verdict.specialisations
    ):
        verdict.category_domain, verdict.specialisations = "", []

    # With no family to compare against there is nothing to widen or narrow, so
    # a brand-new profile is never "a new node". Left as-is the classifier
    # compares against empty text and can report a structural change that does
    # not exist, which would scare the user into a confirmation they do not need.
    if not family:
        verdict.change = "none"
        verdict.new_family_required = False

    related = (
        [f.family_id for f in store.related_families(
            verdict.category_domain, verdict.specialisations)]
        if verdict.specialisations else []
    )
    return {
        "change": verdict.change,
        "new_family_required": verdict.new_family_required,
        "about": verdict.about,
        "category_domain": verdict.category_domain,
        "specialisations": verdict.specialisations,
        "explanation": verdict.explanation,
        "related_families": related,
    }


@router.post("/profile/apply")
async def apply_profile(payload: ApplyProfileRequest):
    """
    Commit an edit. `accept=false` writes NOTHING — that is the whole point of
    splitting classify from apply.
    """
    if not payload.accept:
        return {"status": "declined", "saved": False}

    text = payload.text.strip()
    if not text:
        raise HTTPException(400, "Profile text cannot be empty")

    family = store.get_family(payload.family_id) if payload.family_id else None

    # ── a brand-new profile: place it, then seed from related families ─────
    if family is None:
        verdict = await classify_profile_change("", text)
        domain = verdict.category_domain or payload.category_domain or ""
        specs = verdict.specialisations or payload.specialisations
        if not taxonomy.is_valid(domain, specs):
            domain, specs = "", []
        created = store.create_family(
            about=verdict.about or payload.about or "Untitled profile",
            category_domain=domain,
            specialisations=specs,
            profile_text=text,
        )
        plan = plan_for_new_family(domain, specs) if specs else None
        active_mod.set_active(created.family_id, None)
        _sync_profile_file(created, "")
        logger.info("🌱 New profile family %s created (about=%r)", created.family_id, created.about)
        return {
            "status": "created",
            "saved": True,
            "family": _summary(created),
            "seed_plan": _plan_dict(plan) if plan else None,
        }

    verdict = await classify_profile_change(family.profile_text, text)

    # ── cosmetic: edit in place ───────────────────────────────────────────
    if not verdict.new_family_required:
        family.profile_text = text
        if verdict.about:
            family.about = verdict.about
        if verdict.category_domain and verdict.specialisations:
            family.category_domain = verdict.category_domain
            family.specialisations = verdict.specialisations
        store.save_family(family)
        _mirror_if_active(family)
        return {"status": "updated", "saved": True, "family": _summary(family),
                "seed_plan": None}

    # ── structural: propose a node, and only build it once accepted ────────
    domain = verdict.category_domain or family.category_domain
    specs = verdict.specialisations or family.specialisations
    if not taxonomy.is_valid(domain, specs):
        domain, specs = family.category_domain, family.specialisations

    child = store.create_family(
        about=verdict.about or family.about,
        category_domain=domain,
        specialisations=specs,
        profile_text=text,
        seeded_from=[family.family_id],
    )
    child.generation = family.generation + 1
    store.save_family(child)

    if verdict.change == "narrowing":
        plan = plan_for_narrowing(family.family_id)
    else:
        plan = plan_for_widening(family.family_id, await _scope_map(family, child))

    current = active_mod.read_active() or {}
    active_mod.set_active(child.family_id, current.get("request_family_id"))
    _sync_profile_file(child, _active_request_text(child.family_id))

    logger.info(
        "🌿 %s: %s -> %s — %s | %s",
        verdict.change, family.family_id, child.family_id, verdict.explanation, plan.summary(),
    )
    return {
        "status": "created",
        "saved": True,
        "family": _summary(child),
        "change": verdict.change,
        "explanation": verdict.explanation,
        "seed_plan": _plan_dict(plan),
    }


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
    if not payload.accept:
        return {"status": "declined", "saved": False}
    if store.get_family(payload.family_id) is None:
        raise HTTPException(404, "Family not found")

    text = payload.text.strip()
    existing = (
        store.get_request_family(payload.request_family_id) if payload.request_family_id else None
    )

    if existing is None:
        created = store.create_request_family(payload.family_id, text)
        _mirror_if_active_by_id(payload.family_id, text)
        return {"status": "created", "saved": True, "request": created.model_dump(mode="json")}

    verdict = await classify_request_change(existing.request_text, text)
    if not verdict.new_family_required:
        existing.request_text = text
        store.save_request_family(existing)
        _mirror_if_active_by_id(payload.family_id, text)
        return {"status": "updated", "saved": True,
                "request": existing.model_dump(mode="json"), "change": verdict.change}

    created = store.create_request_family(
        payload.family_id, text, seeded_from=[existing.request_family_id]
    )
    active_mod.set_active(payload.family_id, created.request_family_id)
    _mirror_if_active_by_id(payload.family_id, text)
    logger.info(
        "🎯 Request %s: %s -> %s — %s",
        verdict.change, existing.request_family_id, created.request_family_id,
        verdict.explanation,
    )
    return {
        "status": "created",
        "saved": True,
        "request": created.model_dump(mode="json"),
        "change": verdict.change,
        "explanation": verdict.explanation,
    }


# ── internals ───────────────────────────────────────────────────────────────
def _mirror_if_active(family: store.Family) -> None:
    current = active_mod.read_active() or {}
    if current.get("family_id") == family.family_id:
        _sync_profile_file(family, _active_request_text(family.family_id))


def _mirror_if_active_by_id(family_id: str, request_text: str) -> None:
    family = store.get_family(family_id)
    if family and (active_mod.read_active() or {}).get("family_id") == family_id:
        _sync_profile_file(family, request_text)


def _plan_dict(plan) -> dict:
    return {
        "change": plan.change,
        "sources": plan.sources,
        "inherit_accepted": len(plan.inherit_accepted),
        "rejudge": len(plan.rejudge),
        "out_of_scope": len(plan.out_of_scope),
        "notes": plan.notes,
    }


def _category_matches(ad_category: str, specialisations: list[str]) -> bool | None:
    """
    Cheap scope test against the ad's own recorded category.

    Returns True/False when the label is decisive, None when it is too vague to
    call — those are the only ads worth spending a model call on.
    """
    label = (ad_category or "").strip().lower()
    if not label or not specialisations:
        return None
    tokens = {t for t in label.replace("/", " ").replace("-", " ").split() if len(t) > 2}
    for spec in specialisations:
        spec_tokens = {t for t in spec.lower().replace("/", " ").split() if len(t) > 2}
        if tokens & spec_tokens:
            return True
    # A concrete category that shares nothing with any specialisation is out.
    if tokens and all(spec_tokens.isdisjoint(tokens) for spec_tokens in
                      ({t for t in s.lower().replace("/", " ").split() if len(t) > 2}
                       for s in specialisations)):
        return all(len(t) > 2 for t in tokens)
    return None


async def _scope_map(parent: store.Family, child: store.Family) -> dict[str, bool]:
    """
    Decide which of the parent's rejected/deferred ads the child can inherit.

    Heuristic first, model only for the genuinely ambiguous ones — one call per
    ad would be expensive, and most labels are decisive.
    """
    specs = child.specialisations or parent.specialisations
    if not specs:
        return {}

    inherited = verdicts.lineage_verdicts(CATEGORY, parent.family_id)
    candidates = [
        (ad_id, payload.get("ad_category", ""))
        for ad_id, payload in inherited.items()
        if payload.get("outcome") in ("rejected", "deferred")
    ]

    scoped: dict[str, bool] = {}
    ambiguous: list[str] = []
    for ad_id, label in candidates:
        verdict = _category_matches(label, specs)
        if verdict is None:
            ambiguous.append(ad_id)
        else:
            scoped[ad_id] = verdict

    model_calls = 0
    if ambiguous:
        logger.info(
            "🔎 %d ad(s) need the model for a scope call (%d resolved by label)",
            len(ambiguous), len(scoped),
        )
        for ad_id in ambiguous:
            payload = inherited[ad_id]
            description = payload.get("reason") or ""
            if not description:
                # No usable text: inherit it rather than silently drop a lead.
                scoped[ad_id] = True
                continue
            try:
                check = await check_scope(description, child)
                scoped[ad_id] = check.in_scope
                model_calls += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("⚠️ Scope check failed for %s (%s) — inheriting", ad_id, exc)
                scoped[ad_id] = True
    if model_calls:
        logger.info("🤖 %d scope check(s) via the model", model_calls)
    return scoped