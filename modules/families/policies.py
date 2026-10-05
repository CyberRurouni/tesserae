"""
policies.py — what a new node inherits, per edit kind.

The whole point of recording lineage instead of copying rows: this module
DECIDES what carries across, and it decides it by walking ancestors. Nothing
is duplicated, so there is no second copy to keep in sync and nothing to repair
if a parent node is later corrected.

Rules, straight from the agreed design:

  none       No new node. The edit is applied in place.

  narrowing  Free. Every inherited verdict still holds — a narrower scope can
             only reject MORE, so nothing previously accepted or rejected
             needs re-deciding. Ads the user already accepted are carried
             across as seen: they have been seen, the user is done with them,
             and their acceptance is preserved forever in the lead list
             regardless of which node they were found under.

  widening   Accepted ads carry across unchanged (a wider scope cannot
             un-accept). Rejected and deferred ads are RE-JUDGED from their
             stored records — no re-scraping. Ad scope is checked first so a
             verdict from an unrelated field is never carried in.

  new family Same as widening, seeded from every family sharing the
             specialisation, with "accepted anywhere wins".
"""

import logging
from dataclasses import dataclass, field

from . import store, verdicts

logger = logging.getLogger(__name__)


@dataclass
class SeedPlan:
    """
    The recipe for a new node. Deliberately inert data — nothing here writes,
    so the user can be shown the plan and decline it before anything moves.
    """

    change: str
    sources: list[str] = field(default_factory=list)
    # Ids inherited without re-judging, bucketed by how they were carried.
    inherit_accepted: list[str] = field(default_factory=list)
    # Ids that must be re-judged from their stored record.
    rejudge: list[str] = field(default_factory=list)
    # Ids dropped outright: out of this family's scope, so not carried at all.
    out_of_scope: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.change}: inherit {len(self.inherit_accepted)} accepted, "
            f"re-judge {len(self.rejudge)}, drop {len(self.out_of_scope)}"
        )


# Seeding is always done for ONE category today; the category is a parameter
# so widening this to several does not mean touching the policy logic.
CATEGORY = "career_jobs"


def _source_ids(family_id: str) -> tuple[set[str], set[str], set[str]]:
    """(accepted, rejected, deferred) ad ids judged under a family."""
    return (
        verdicts.verdicts_for(CATEGORY, family_id, "accepted"),
        verdicts.verdicts_for(CATEGORY, family_id, "rejected"),
        verdicts.verdicts_for(CATEGORY, family_id, "deferred"),
    )


def plan_for_narrowing(family_id: str) -> SeedPlan:
    """
    Nothing is re-judged. Everything is inherited as 'already seen'.

    The new node simply records the old one as an ancestor, and the skip check
    consults ancestors — so every ad the old node had already judged is skipped
    in the new one automatically, at zero cost.
    """
    accepted, rejected, deferred = _source_ids(family_id)
    plan = SeedPlan(
        change="narrowing",
        sources=[family_id],
        inherit_accepted=sorted(accepted),
        rejudge=[],
        notes=[
            "Narrowing inherits every verdict — a smaller scope can only reject more.",
            "Previously accepted ads are carried as seen; you are done with them.",
            "Nothing is re-judged and nothing is re-scraped.",
        ],
    )
    logger.info("🌱 Narrowing plan from %s: %s", family_id, plan.summary())
    return plan


def plan_for_widening(
    family_id: str,
    scoped: dict[str, bool] | None = None,
) -> SeedPlan:
    """
    Accepted carry; rejected and deferred are re-judged.

    `scoped` optionally maps ad_id -> in_scope from the zero-creativity scope
    check. Anything judged out of scope is not carried at all: "deferred can
    never be rejected" holds only while an ad stays inside the family's scope,
    and leaving a foreign ad in the pool would have its TTL renewed forever.
    """
    scoped = scoped or {}
    accepted, rejected, deferred = _source_ids(family_id)

    out_of_scope = [ad for ad in (rejected | deferred) if scoped.get(ad) is False]
    candidates = sorted((rejected | deferred) - set(out_of_scope))

    plan = SeedPlan(
        change="widening",
        sources=[family_id],
        inherit_accepted=sorted(accepted),
        rejudge=candidates,
        out_of_scope=sorted(out_of_scope),
        notes=[
            "Widening keeps previously accepted ads — a wider scope cannot un-accept.",
            f"{len(candidates)} rejected/deferred ad(s) will be re-judged from their stored records.",
            "Re-judging costs one model call each; nothing is re-scraped.",
        ],
    )
    if out_of_scope:
        plan.notes.append(
            f"{len(out_of_scope)} ad(s) fall outside the new scope and are not carried in."
        )
    logger.info("🌳 Widening plan from %s: %s", family_id, plan.summary())
    return plan


def plan_for_new_family(domain: str, specialisations: list[str]) -> SeedPlan:
    """
    Brand-new node seeded from every family sharing the specialisation.

    "Accepted anywhere wins" — if one source family accepted an ad and another
    rejected it, the ad is carried as accepted. The user has already seen it,
    and re-surfacing it is the exact failure this design exists to prevent.
    """
    sources = [f.family_id for f in store.related_families(domain, specialisations)]
    if not sources:
        return SeedPlan(
            change="new_family",
            notes=["No related families found — this node starts empty."],
        )

    accepted_any: set[str] = set()
    rejudge: set[str] = set()
    for src in sources:
        accepted, rejected, deferred = _source_ids(src)
        accepted_any |= accepted
        rejudge |= rejected | deferred

    rejudge -= accepted_any  # accepted anywhere wins

    plan = SeedPlan(
        change="new_family",
        sources=sources,
        inherit_accepted=sorted(accepted_any),
        rejudge=sorted(rejudge),
        notes=[
            f"Seeded from {len(sources)} related famil{'y' if len(sources) == 1 else 'ies'}.",
            f"{len(accepted_any)} accepted ad(s) carried across (accepted anywhere wins).",
            f"{len(rejudge)} ad(s) queued for re-judging from stored records.",
        ],
    )
    logger.info("🌲 New family plan for %s/%s: %s", domain, specialisations, plan.summary())
    return plan


def seed_plan_for_edit(change: str, **kwargs) -> SeedPlan:
    """Dispatch used by the API layer so the endpoint stays thin."""
    if change == "narrowing":
        return plan_for_narrowing(kwargs["family_id"])
    if change == "widening":
        return plan_for_widening(kwargs["family_id"], kwargs.get("scoped"))
    if change == "new_family":
        return plan_for_new_family(kwargs["domain"], kwargs.get("specialisations") or [])
    return SeedPlan(change="none", notes=["No structural change — nothing to seed."])