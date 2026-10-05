"""
seen.py — the two "don't process this twice" memories.

They answer different questions and must not be confused:

  SeenIndex   — "does this ad already have a FINAL verdict under this
                 category+profile?"  Lives in Redis DB 1 (verdict_broker) and
                 is mirrored in memory so the hot path (one lookup per scraped
                 card) never makes a Redis round trip. The mirror is updated
                 the moment we mark, so a verdict reached mid-run is visible to
                 later keywords and later cycles of the SAME run.

  RunSeenIds  — "has anything in the CURRENT run already looked at this ad?"
                 Lives in Redis DB 2 (run_state_broker), TTL'd to the run, and
                 is shared by every worker. This is what makes keyword-level
                 parallelism safe: two workers on different keywords can meet
                 the same ad and only harvest it once.

Both are deliberately dumb. The difference is scope and lifetime: SeenIndex is
"this ad was already judged, ever, under this lens" and outlives the run;
RunSeenIds is "we saw it during this run" and dies with it.
"""

import logging

from core import (
    compute_profile_hashes,
    run_state_broker,
    safe_redis_operation,
    verdict_broker,
)

logger = logging.getLogger(__name__)

SEEN_AD_PREFIX = "tesserae:seen_ad"
RUN_SEEN_PREFIX = "tesserae:run_seen"

# How long a run's seen-set survives. Long enough for a multi-hour run with
# 1-3 workers, short enough that an abandoned run cleans itself up.
RUN_SEEN_TTL_SECONDS = 8 * 3600

# Redis SCANN hints — page through the namespace without blocking the server.
_SCAN_COUNT = 500


def seen_ad_key(category: str, ad_id: str, about_hash: str, filter_hash: str) -> str:
    return f"{SEEN_AD_PREFIX}:{category}:{about_hash}:{filter_hash}:{ad_id}"


def seen_ad_pattern(category: str, about_hash: str, filter_hash: str) -> str:
    """Match every verdict key in one category+profile namespace."""
    return f"{SEEN_AD_PREFIX}:{category}:{about_hash}:{filter_hash}:*"


class SeenIndex:
    """
    In-memory mirror of the FINAL-verdict namespace for one category+profile.

    The namespace is keyed on hashes of the two profile text files, so editing
    either one yields an empty mirror — that is the intended behaviour (a
    changed profile is a changed lens), not a bug to paper over.
    """

    def __init__(self, category: str, filters=None):
        self.category = category
        self.filters = filters
        about_hash, filter_hash = compute_profile_hashes()
        self.about_hash = about_hash
        self.filter_hash = filter_hash
        self._ids: set[str] = set()
        self._loaded = False

    # ── loading ────────────────────────────────────────────────────────────
    def load(self, force: bool = False) -> "SeenIndex":
        """One SCAN of the namespace instead of one EXISTS per card."""
        if self._loaded and not force:
            return self
        found: set[str] = set()
        try:
            for key in verdict_broker.scan_iter(
                match=seen_ad_pattern(self.category, self.about_hash, self.filter_hash),
                count=_SCAN_COUNT,
            ):
                found.add(key.rsplit(":", 1)[-1])
        except Exception as exc:  # noqa: BLE001 - never let dedup break a scrape
            logger.warning("⚠️ Could not preload verdict namespace (%s) — per-card checks stay live", exc)
            self._loaded = True
            return self
        self._ids = found
        self._loaded = True
        logger.info(
            "🧠 Verdict namespace loaded: %d ad(s) already final under this profile",
            len(found),
        )
        return self

    # ── queries ────────────────────────────────────────────────────────────
    def has(self, ad_id: str) -> bool:
        """True when this ad already reached a FINAL verdict here."""
        if not self._loaded:
            self.load()
        return ad_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)

    # ── writes ─────────────────────────────────────────────────────────────
    def mark(self, ad_id: str, ttl: int | None = None) -> bool:
        """
        Record a FINAL verdict: Redis first (source of truth), then the mirror.

        Returns True when this call is what established the verdict, i.e. the
        same "newly marked" count the run logs have always reported.
        """
        if not self._loaded:
            self.load()
        was_new = ad_id not in self._ids
        key = seen_ad_key(self.category, ad_id, self.about_hash, self.filter_hash)
        if ttl is None:
            safe_redis_operation(verdict_broker.set, key, 1)
        else:
            safe_redis_operation(verdict_broker.set, key, 1, ex=ttl)
        self._ids.add(ad_id)
        return was_new


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


_INDEX_CACHE: dict[tuple[str, str, str], SeenIndex] = {}


def get_seen_index(category: str, filters=None, refresh: bool = False) -> SeenIndex:
    """
    One shared mirror per category+profile namespace per process.

    Cached because a run walks many keywords and cycles: reloading per cycle
    would SCAN the namespace again for no benefit, the mirror only grows.
    """
    about_hash, filter_hash = compute_profile_hashes()
    cache_key = (category, about_hash, filter_hash)
    index = _INDEX_CACHE.get(cache_key)
    if index is None:
        index = SeenIndex(category, filters)
        _INDEX_CACHE[cache_key] = index
    if refresh:
        index.load(force=True)
    else:
        index.load()
    return index


def reset_index_cache() -> None:
    """Tests and profile edits: forget the mirrors entirely."""
    _INDEX_CACHE.clear()