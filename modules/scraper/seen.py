"""
seen.py — the run-scoped "don't look at this twice" memory.

RunSeenIds lives in Redis DB 2 (run_state_broker), TTL'd to the run, and is
shared by every worker. This is what makes keyword-level parallelism safe: two
workers on different keywords can meet the same ad and only harvest it once.

It is deliberately dumb. "Which verdicts already exist for this profile?" is a
different question with a different home — see modules/verdicts.py.

This file used to also carry SeenIndex, the original dedup: one Redis key per
ad, filed under a namespace built from hashes of the user's profile TEXT. That
meant rewording your profile rotated the key space and every ad you had already
judged looked unseen again — you paid to re-scrape and re-judge the same ads
after fixing a typo. Verdicts are now filed against a stable profile id in
modules/verdicts.py, which is why nothing here needs to know what a profile is.
"""

import logging

from core import (
    run_state_broker,
    safe_redis_operation,
)

logger = logging.getLogger(__name__)

SEEN_AD_PREFIX = "tesserae:seen_ad"
RUN_SEEN_PREFIX = "tesserae:run_seen"

# How long a run's seen-set survives. Long enough for a multi-hour run with
# 1-3 workers, short enough that an abandoned run cleans itself up.
RUN_SEEN_TTL_SECONDS = 8 * 3600

# Redis SCANN hints — page through the namespace without blocking the server.
_SCAN_COUNT = 500



class RunSeenIds:
    """
    Ids already looked at during THIS run, shared across workers.

    `shared` gates the cross-worker half. With one worker the local set is the
    whole truth and the Redis mirror exists only for resume/inspection, so we
    skip the per-card SISMEMBER that would otherwise cost a round trip to learn
    something we already know. Flip it on when parallel workers are enabled and
    the same object becomes the cross-worker channel.
    """

    def __init__(self, run_id: str | None = None, shared: bool = False, ttl: int = RUN_SEEN_TTL_SECONDS):
        self.run_id = run_id
        self.shared = shared
        self.ttl = ttl
        self._local: set[str] = set()
        self._redis_backed = False
        if run_id:
            self.key = f"{RUN_SEEN_PREFIX}:{run_id}"
            self._ensure_key()
        else:
            # No run id (smoke runner, tests): memory only.
            self.key = None

    def _ensure_key(self) -> None:
        if not self.key:
            return
        # Deliberately no sentinel write here. SET would create a STRING and
        # every later SADD would fail with WRONGTYPE, silently turning the
        # cross-worker channel into a no-op. The key is created by the first
        # SADD instead, which sets the type and the TTL together.
        if safe_redis_operation(run_state_broker.exists, self.key):
            safe_redis_operation(run_state_broker.expire, self.key, self.ttl)
        self._redis_backed = True

    def has(self, ad_id: str) -> bool:
        if ad_id in self._local:
            return True
        if self.shared and self._redis_backed:
            return safe_redis_operation(run_state_broker.sismember, self.key, ad_id) == 1
        return False

    def add_many(self, ad_ids) -> None:
        """Batch one scroll step's worth of ids — a single SADD round trip."""
        new = [ad_id for ad_id in ad_ids if ad_id and ad_id not in self._local]
        if not new:
            return
        self._local.update(new)
        if self._redis_backed:
            safe_redis_operation(run_state_broker.sadd, self.key, *new)
            # Refresh the TTL so a long run never expires mid-flight.
            safe_redis_operation(run_state_broker.expire, self.key, self.ttl)

    def __contains__(self, ad_id: str) -> bool:
        return self.has(ad_id)

    def __len__(self) -> int:
        return len(self._local)

    def clear(self) -> None:
        self._local.clear()
        if self._redis_backed:
            safe_redis_operation(run_state_broker.delete, self.key)

