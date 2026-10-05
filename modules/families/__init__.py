"""
families/ — profile families, request families, and their lead lists.

Three concepts, one module:

  taxonomy   — the closed category vocabulary. The AI chooses within it, never
               invents it, so related-family discovery stays reliable.
  store      — JSON-first persistence (families.json, per-family lead lists)
               with a Redis mirror for fast membership checks.
  classifier — the only model calls, split so the one that decides whether an
               inherited verdict carries stays deterministic.

Seeding policy, which is why lineage exists (see store.ancestry):

  edit kind    accepted ads      rejected / deferred ads
  -----------  ----------------  ---------------------------
  none         (edit in place)   unchanged
  narrowing    inherited as-is   inherited as-is — nothing re-judged
  widening     inherited as-is   RE-JUDGED from the stored records
  new family   inherited (accept wins across sources)  RE-JUDGED

Narrowing is deliberately free: every inherited verdict still holds, because a
narrower scope can only ever reject more. Widening is the only case that pays
for re-judging, and it re-judges from stored ad records rather than re-scraping.
"""

from . import store, taxonomy, verdicts
from .classifier import (
    check_scope,
    classify_profile_change,
    classify_request_change,
)
from .policies import seed_plan_for_edit

__all__ = [
    "taxonomy",
    "store",
    "verdicts",
    "classify_profile_change",
    "classify_request_change",
    "check_scope",
    "seed_plan_for_edit",
]