"""
collection.py — the keyword collection's persistence layer (Redis db3).

Redis is TRUTH; the runtime collection is just its mirror.

  - keyword keys:  tesserae:kw:<category>:<scope>:<hash>       value = KeywordStats
                   JSON. The record holds the keyword's whole life — every
                   search, every lead — plus `reuse_at`, the cooldown.
                   TTL = max(cooldown, STATS_RECORD_TTL): the cooldown
                   expires, the evidence does not.
  - prompt keys:   tesserae:prompt:<category>:<scope>:<hash>  value = prompt JSON,
                   TTL = expiry (Actor 4 decides)
  - harvest flag:  tesserae:state:<category>:<scope>:harvest  (all-covered mode)

<scope> is the profile id. See the module note below for why the pool is not
shared across profiles.

A keyword's record is deliberately NOT dropped when its cooldown lapses.
"Searchable again" is a question about the pool; "what did this keyword do" is
a question about the past, and the keyword generator needs the second one to
learn. load_collection therefore separates the two: `keywords` is everything
we know, `searchable` is what may be searched now.

The bloat solution lives here too: when a coverage review forms an
exclusion prompt, its composing keywords are DELETED (their keys vanish)
and the prompt — one compressed string — takes their place.
"""

"""
collection.py — the keyword pool the AI generates into and harvests from.

Scoping: category AND profile.

The collection used to be keyed by category alone, which quietly shared one
pool between every profile. Two profiles then fought over the same keywords:
"full stack AI systems lead", generated for the AI Systems Architect profile,
kept getting searched under Python developer — and the run looked like it was
using the wrong profile, when in fact it was using the right one against
another profile's backlog. Keywords are cheap to regenerate and wrong ones cost
a captcha each, so each profile gets its own pool.

`profile_id` may be None, which keeps the old behaviour under an explicit
"unscoped" namespace rather than silently falling back to every profile's keys.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

from core import KeywordStats, KeywordStatus, keyword_broker, safe_redis_operation

logger = logging.getLogger(__name__)

KW_KEY = "tesserae:kw:{category}:{scope}:{ident}"
PROMPT_KEY = "tesserae:prompt:{category}:{scope}:{ident}"
HARVEST_KEY = "tesserae:state:{category}:{scope}:harvest"

UNSCOPED = "unscoped"


KEYWORD_IDENT_CACHE: dict[str, str] = {}

# How a keyword's record is forgotten, if at all.
#
# NOT A TTL. A record carries both the cooldown (when to search it again) and
# the evidence (what it returned, how many times, how many leads). Those expire
# differently and must not be tied together: a Redis TTL on the record deletes
# the evidence the moment the cooldown lapses, which is how a proven keyword
# quietly lost its whole history. So the cooldown lives in `reuse_at` and
# decides searchable membership, and the record simply never expires.
#
# Unbounded growth is deliberate and cheap: a record is ~300 bytes and keywords
# are only created deliberately, ~12 per cycle. Anything genuinely stale ages
# out of relevance on its own — it falls below the harvest ranking and stops
# being chosen. Forgetting is a decision, not an accident of a clock.
STATS_RECORD_TTL = None

# How many keyword histories the generator is shown per cycle. The proven
# performers are always included regardless of where they sort; this caps the
# rest, because a wall of 180 entries that are all zeros teaches nothing and
# buries the handful that are not.
FEEDBACK_HISTORY_LIMIT = 25


def _scope(profile_id: str | None) -> str:
    """Namespace segment for a profile's pool."""
    return profile_id or UNSCOPED


def _ident(text: str) -> str:
    """
    Stable identity for a keyword/prompt — case- and whitespace-
    insensitive, INCLUDING internal whitespace runs ('Python  Devs '
    and 'python devs' are the same keyword). Without collapsing internal
    runs, save/delete disagree and prompts fail to delete their keywords.
    """
    cached = KEYWORD_IDENT_CACHE.get(text)
    if cached:
        return cached
    digest = hashlib.sha1(" ".join(text.split()).lower().encode("utf-8")).hexdigest()[:16]
    KEYWORD_IDENT_CACHE[text] = digest
    return digest


def _kw_key(category: str, profile_id: str | None, keyword: str) -> str:
    return KW_KEY.format(
        category=category, scope=_scope(profile_id), ident=_ident(keyword)
    )


def _ttl_seconds(until: datetime) -> int:
    """Seconds from now until `until` (never below 1)."""
    return max(1, int((until - datetime.now(timezone.utc)).total_seconds()))


def _merge(existing: KeywordStats | None, incoming: KeywordStats) -> KeywordStats:
    """
    Fold one cycle's results into a keyword's lifetime record.

    The single place accumulation happens, so the two callers that need it —
    the one feeding Actor 2 and the one writing to Redis — cannot drift apart
    and double-count.

    `incoming` always describes ONE search (runs_searched == 1). Summing the
    counters and taking the newer timestamp is what turns it into a record of
    the keyword's life rather than of its most recent afternoon.
    """
    if existing is None:
        return incoming.model_copy(deep=True)

    merged = existing.model_copy(deep=True)
    merged.runs_searched += incoming.runs_searched
    merged.ads_fetched += incoming.ads_fetched
    merged.relevant_ads += incoming.relevant_ads
    if incoming.last_relevant_hit_at:
        if not merged.last_relevant_hit_at or incoming.last_relevant_hit_at > merged.last_relevant_hit_at:
            merged.last_relevant_hit_at = incoming.last_relevant_hit_at
    # The latest search wins: it is the one that decides when this keyword is
    # searchable again, and the one "the last batch" is identified by.
    if incoming.last_searched_at:
        if not merged.last_searched_at or incoming.last_searched_at >= merged.last_searched_at:
            merged.last_searched_at = incoming.last_searched_at
    if incoming.category and not merged.category:
        merged.category = incoming.category
    # A keyword that has ever produced a lead is alive, whatever today looked
    # like. Judging it on the last cycle alone is how a proven keyword retires.
    merged.status_hint = KeywordStatus.alive if merged.relevant_ads > 0 else KeywordStatus.new
    return merged


def load_keyword(keyword: str, category: str, profile_id: str | None = None) -> KeywordStats | None:
    """The stored record for one keyword, or None if it has expired entirely."""
    raw = safe_redis_operation(keyword_broker.get, _kw_key(category, profile_id, keyword))
    if not raw:
        return None
    try:
        return KeywordStats.model_validate_json(raw)
    except Exception:
        logger.warning("⚠️ Unreadable keyword payload for %r — treated as absent", keyword)
        return None


def merge_with_history(
    stats: list[KeywordStats], category: str, profile_id: str | None = None
) -> list[KeywordStats]:
    """
    Fold each keyword's stored history into this cycle's fresh results.

    Read-only — nothing is written. Used to hand Actor 2 the accumulated
    picture, which its prompt has always asked for ("how many times it has
    been searched") and has never actually been given.
    """
    return [_merge(load_keyword(s.keyword, category, profile_id), s) for s in stats]


def save_keyword(stats: KeywordStats, category: str, reuse_at: datetime,
                 profile_id: str | None = None) -> KeywordStats:
    """
    Store a searched keyword, merged with its history. Returns the merged record.

    `reuse_at` is the COOLDOWN — when this keyword may be searched again — and
    lives inside the record. The record itself is never given a Redis TTL: the
    cooldown decides searchable membership in memory (see load_collection), so a
    TTL here would only ever delete evidence.
    """
    merged = _merge(load_keyword(stats.keyword, category, profile_id), stats)
    merged.reuse_at = reuse_at
    key = _kw_key(category, profile_id, merged.keyword)
    safe_redis_operation(keyword_broker.set, key, merged.model_dump_json())
    return merged


def rank_for_harvest(stats: list[KeywordStats]) -> list[KeywordStats]:
    """
    Order keywords by how much a re-search is likely to be worth.

    Accepted leads first, then ads found as the tie-break — a keyword that
    surfaces ads nobody wanted was still a working search, while one that
    surfaced nothing was never a working search at all. The name is the final
    tie-break purely so the order is stable between calls; otherwise the UI and
    the orchestrator could disagree about what "top 10" means.
    """
    return sorted(
        stats,
        key=lambda s: (-s.relevant_ads, -s.ads_fetched, s.keyword.lower()),
    )


def last_batch(stats: list[KeywordStats]) -> list[KeywordStats]:
    """
    Every keyword searched in the most recent cycle.

    A cycle stamps all its keywords with one instant, so the newest stamp
    identifies the batch exactly. Used by the generator to see its own latest
    attempt next to its proven performers — the gap between the two is what
    tells it whether it is drifting away from what works.
    """
    stamps = [s.last_searched_at for s in stats if s.last_searched_at]
    if not stamps:
        return []
    newest = max(stamps)
    return [s for s in stats if s.last_searched_at == newest]


def resolve_harvest(
    searchable: list[KeywordStats],
    limit: int = 20,
    chosen: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """
    Decide which keywords a harvest round re-searches.

    Returns (keywords_to_search, notes). An explicit selection wins over the
    limit, because a person naming a keyword is a stronger signal than a
    number. Names that are not in the searchable pool come back in `notes`
    rather than being searched silently — searching a keyword that is mid
    cooldown would defeat the cooldown the user is trying to respect.

    A keyword that produced nothing is not ranked out of existence here: if the
    limit is generous enough it can still be selected, since a keyword that
    once returned ads may simply have had a quiet month.
    """
    pool = {s.keyword.strip().lower(): s for s in searchable}
    notes: list[str] = []

    if chosen:
        wanted: list[str] = []
        for raw in chosen:
            name = raw.strip()
            if not name:
                continue
            match = pool.get(name.lower())
            if match is None:
                notes.append(f"{name!r} is not in the searchable pool (cooling down, or never searched)")
                continue
            if match.keyword not in wanted:
                wanted.append(match.keyword)
        return wanted, notes

    if limit <= 0:
        return [], ["harvest limit is 0 — no keywords re-searched"]

    ranked = rank_for_harvest(searchable)
    return [s.keyword for s in ranked[:limit]], notes


def save_prompt(prompt: str, covered_keywords: list[str], category: str, expires_at: datetime,
                profile_id: str | None = None) -> None:
    """Store an exclusion prompt; TTL = when its territory reopens."""
    key = PROMPT_KEY.format(category=category, scope=_scope(profile_id), ident=_ident(prompt))
    payload = json.dumps(
        {
            "prompt": prompt,
            "covered_keywords": covered_keywords,
            "expires_at": expires_at.isoformat(),
        }
    )
    safe_redis_operation(keyword_broker.set, key, payload, ex=_ttl_seconds(expires_at))


def delete_keywords(category: str, keywords: list[str],
                    profile_id: str | None = None) -> int:
    """Remove composing keywords after a prompt absorbed them (both stores)."""
    deleted = 0
    for kw in keywords:
        key = _kw_key(category, profile_id, kw)
        if safe_redis_operation(keyword_broker.delete, key):
            deleted += 1
    return deleted


def load_collection(category: str, profile_id: str | None = None) -> dict:
    """
    Rebuild the collection from Redis.

    Returns three things, because they answer different questions:

      keywords   — every keyword we know about, searchable or not. This is the
                   generator's memory: what has been tried, and what it did.
      searchable — the subset whose cooldown has elapsed. THIS is the live
                   pool that harvest mode re-searches and that the coverage
                   reviewer sizes itself against.
      prompts    — live exclusion prompts, still TTL'd (a prompt's expiry IS
                   its meaning, so it has no reason to outlive itself).

    Before the cooldown and the record were separated, "live" was whatever had
    not expired, so forgetting a keyword also meant forgetting what it proved.
    """
    scope = _scope(profile_id)
    now = datetime.now(timezone.utc)

    keywords: list[KeywordStats] = []
    for key in safe_redis_operation(
        keyword_broker.keys, KW_KEY.format(category=category, scope=scope, ident="*")
    ) or []:
        raw = safe_redis_operation(keyword_broker.get, key)
        if raw:
            try:
                keywords.append(KeywordStats.model_validate_json(raw))
            except Exception:
                logger.warning("⚠️ Unreadable keyword payload at %s — skipped", key)

    searchable = [
        s for s in keywords if s.reuse_at is None or s.reuse_at <= now
    ]

    prompts: list[dict] = []
    for key in safe_redis_operation(
        keyword_broker.keys, PROMPT_KEY.format(category=category, scope=scope, ident="*")
    ) or []:
        raw = safe_redis_operation(keyword_broker.get, key)
        if raw:
            try:
                prompts.append(json.loads(raw))
            except Exception:
                logger.warning("⚠️ Unreadable prompt payload at %s — skipped", key)

    logger.info(
        "📚 Collection (%s): %d keyword(s) known (%d searchable now), %d live prompt(s)",
        scope, len(keywords), len(searchable), len(prompts),
    )
    return {"keywords": keywords, "searchable": searchable, "prompts": prompts}


def set_harvest_flag(category: str, days: int, profile_id: str | None = None) -> None:
    """All skills covered — suppress generation for `days` (harvest mode)."""
    key = HARVEST_KEY.format(category=category, scope=_scope(profile_id))
    safe_redis_operation(keyword_broker.set, key, "1", ex=max(1, days) * 86400)


def harvest_active(category: str, profile_id: str | None = None) -> bool:
    """True while the harvest-mode window is open for this profile."""
    key = HARVEST_KEY.format(category=category, scope=_scope(profile_id))
    return safe_redis_operation(keyword_broker.exists, key) == 1
