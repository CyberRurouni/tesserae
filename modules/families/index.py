"""
index.py — in-memory mirror of a family's verdicts, for the scrape hot path.

One lookup per scraped card decides whether to skip. Asking Redis per card
costs far more than the work it saves, so a run preloads everything judged for
its family (and its ancestors) once, then answers from memory.

The mirror is a CACHE, never the truth: Redis holds the verdicts, and this is
rebuilt per run. Writes go to Redis first and are applied to the mirror too, so
a verdict recorded mid-run is visible to later keywords and later cycles of the
same run without a reload.
"""

import logging

from . import verdicts
from . import store

logger = logging.getLogger(__name__)


class FamilyVerdictIndex:
    """Everything already judged for one family, including its ancestors."""

    def __init__(self, category: str, family_id: str):
        self.category = category
        self.family_id = family_id
        self._known: set[str] = set()
        self._loaded = False

    # ── loading ────────────────────────────────────────────────────────────
    def load(self, force: bool = False) -> "FamilyVerdictIndex":
        if self._loaded and not force:
            return self
        ancestors = store.ancestry(self.family_id)
        self._known = verdicts.known_ids(self.category, self.family_id)
        self._loaded = True
        logger.info(
            "🧠 Verdict mirror: %d ad(s) already judged for %s%s",
            len(self._known),
            self.family_id,
            f" (inheriting {len(ancestors)} ancestor(s))" if ancestors else "",
        )
        return self

    # ── queries ────────────────────────────────────────────────────────────
    def has(self, ad_id: str) -> bool:
        if not self._loaded:
            self.load()
        return ad_id in self._known

    def outcome(self, ad_id: str) -> str | None:
        if not self.has(ad_id):
            return None
        record = verdicts.get_verdict(self.category, self.family_id, ad_id)
        return record.get("outcome") if record else None

    def __contains__(self, ad_id: str) -> bool:
        return self.has(ad_id)

    def __len__(self) -> int:
        if not self._loaded:
            self.load()
        return len(self._known)

    def stats(self) -> dict[str, int]:
        return verdicts.stats(self.category, self.family_id)

    # ── writes ─────────────────────────────────────────────────────────────
    def record(self, ad_id: str, outcome: str, reason: str = "", ad_category: str = "") -> None:
        verdicts.record_verdict(self.category, self.family_id, ad_id, outcome, reason, ad_category)
        self._known.add(ad_id)


_INDEX_CACHE: dict[tuple[str, str], FamilyVerdictIndex] = {}


def get_family_index(category: str, family_id: str) -> FamilyVerdictIndex:
    """One mirror per family per process. The mirror only grows within a run."""
    cache_key = (category, family_id)
    index = _INDEX_CACHE.get(cache_key)
    if index is None:
        index = FamilyVerdictIndex(category, family_id)
        _INDEX_CACHE[cache_key] = index
    index.load()
    return index


def reset_cache() -> None:
    _INDEX_CACHE.clear()