"""
verdicts.py — per-family verdicts, read through lineage.

This is where the duplicate problem is actually solved. The old namespace was

    tesserae:seen_ad:{category}:{about_hash}:{filter_hash}:{ad_id}

— a content hash of the profile text, which meant a one-word edit silently
rotated the entire key space and every ad became "unseen" again.

Now a verdict belongs to a FAMILY, and a new node names the families it
descends from rather than copying their rows. The skip check asks "has this ad
been judged under this family OR any of its ancestors?" — so narrowing and
widening share one mechanism, and a corrected parent is immediately reflected
in every child with nothing to re-sync.

Outcomes:
  accepted  — wanted now
  rejected  — outside the family's acceptable set (permanent)
  deferred  — inside the family, not wanted under the current request

`deferred` can never become `rejected` while it stays in scope; see
modules/families/policies.py for how a scope check bounds that.
"""

import json
import logging

from core import run_state_broker, safe_redis_operation

from . import store

logger = logging.getLogger(__name__)

VERDICT_KEY = "tesserae:verdict:{category}:{family_id}"
OUTCOMES = ("accepted", "rejected", "deferred")

# Verdict records are permanent by design — a rejected ad stays rejected. This
# TTL is only a guard against unbounded growth, not an expiry policy.
VERDICT_TTL_SECONDS = 365 * 24 * 3600


def _key(category: str, family_id: str) -> str:
    return VERDICT_KEY.format(category=category, family_id=family_id)


def record_verdict(
    category: str,
    family_id: str,
    ad_id: str,
    outcome: str,
    reason: str = "",
    ad_category: str = "",
) -> None:
    """
    Store one ad's outcome for a family.

    A hash keyed by ad_id, so re-judging the same ad overwrites rather than
    accumulating — the exact defect in the old append-only store.
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    if not ad_id:
        return
    safe_redis_operation(
        run_state_broker.hset,
        _key(category, family_id),
        ad_id,
        json.dumps({"outcome": outcome, "reason": reason, "ad_category": ad_category},
                   ensure_ascii=False),
    )
    safe_redis_operation(run_state_broker.expire, _key(category, family_id), VERDICT_TTL_SECONDS)


def get_verdict(category: str, family_id: str, ad_id: str) -> dict | None:
    raw = safe_redis_operation(run_state_broker.hget, _key(category, family_id), ad_id)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _own_verdicts(category: str, family_id: str) -> dict[str, dict]:
    raw = safe_redis_operation(run_state_broker.hgetall, _key(category, family_id)) or {}
    out: dict[str, dict] = {}
    for ad_id, payload in raw.items():
        try:
            out[ad_id] = json.loads(payload)
        except json.JSONDecodeError:
            continue
    return out


def lineage_verdicts(category: str, family_id: str) -> dict[str, dict]:
    """
    Every ad judged for this family or any ancestor.

    A verdict held by an ancestor counts, EXCEPT when this node has its own
    verdict for the same ad — the node's own decision is the more specific one
    and must win.
    """
    merged: dict[str, dict] = {}
    for ancestor in reversed(store.ancestry(family_id)):
        merged.update(_own_verdicts(category, ancestor))
    merged.update(_own_verdicts(category, family_id))
    return merged


def verdicts_for(category: str, family_id: str, outcome: str) -> set[str]:
    """Ids with this outcome, following lineage."""
    return {
        ad_id
        for ad_id, payload in lineage_verdicts(category, family_id).items()
        if payload.get("outcome") == outcome
    }


def has_verdict(category: str, family_id: str, ad_id: str) -> bool:
    """
    The hot path — one lookup, called per scraped card.

    Own verdict first, then ancestors. Returns a bool so callers can skip; use
    get_verdict() when the outcome itself matters.
    """
    if safe_redis_operation(run_state_broker.hexists, _key(category, family_id), ad_id):
        return True
    for ancestor in store.ancestry(family_id):
        if safe_redis_operation(run_state_broker.hexists, _key(category, ancestor), ad_id):
            return True
    return False


def known_ids(category: str, family_id: str) -> set[str]:
    """Every id judged under this family or an ancestor — for in-memory preload."""
    return set(lineage_verdicts(category, family_id))


def stats(category: str, family_id: str) -> dict[str, int]:
    counts = {outcome: 0 for outcome in OUTCOMES}
    for payload in lineage_verdicts(category, family_id).values():
        outcome = payload.get("outcome")
        if outcome in counts:
            counts[outcome] += 1
    return counts