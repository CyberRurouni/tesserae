"""
collection.py — the keyword collection's persistence layer (Redis db3).

Redis is TRUTH; the runtime collection is just its mirror.

  - keyword keys:  tesserae:kw:<category>:<scope>:<hash>       value = KeywordStats
                   JSON, TTL = reuse timestamp (Actor 2 decides, script converts)
  - prompt keys:   tesserae:prompt:<category>:<scope>:<hash>  value = prompt JSON,
                   TTL = expiry (Actor 4 decides)
  - harvest flag:  tesserae:state:<category>:<scope>:harvest  (all-covered mode)

<scope> is the profile id. See the module note below for why the pool is not
shared across profiles.

Expiry is SILENT by design: a key that's gone means its territory is
searchable again — no deletion job, no cron. A crashed script loses
nothing: the collection rebuilds by scanning db3 (dump-from-redis restart).

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

from core import KeywordStats, keyword_broker, safe_redis_operation

logger = logging.getLogger(__name__)

KW_KEY = "tesserae:kw:{category}:{scope}:{ident}"
PROMPT_KEY = "tesserae:prompt:{category}:{scope}:{ident}"
HARVEST_KEY = "tesserae:state:{category}:{scope}:harvest"

UNSCOPED = "unscoped"


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
    return hashlib.sha1(" ".join(text.split()).lower().encode("utf-8")).hexdigest()[:16]


def _ttl_seconds(until: datetime) -> int:
    """Seconds from now until `until` (never below 1)."""
    return max(1, int((until - datetime.now(timezone.utc)).total_seconds()))


def save_keyword(stats: KeywordStats, category: str, reuse_at: datetime,
                 profile_id: str | None = None) -> None:
    """Store a searched keyword with stats; TTL = when it may be reused."""
    key = KW_KEY.format(category=category, scope=_scope(profile_id), ident=_ident(stats.keyword))
    safe_redis_operation(keyword_broker.set, key, stats.model_dump_json(), ex=_ttl_seconds(reuse_at))


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
        key = KW_KEY.format(category=category, scope=_scope(profile_id), ident=_ident(kw))
        if safe_redis_operation(keyword_broker.delete, key):
            deleted += 1
    return deleted


def load_collection(category: str, profile_id: str | None = None) -> dict:
    """
    Rebuild the runtime collection from Redis: live (non-expired) keywords
    with stats + live exclusion prompts. Expired keys are simply absent.
    """
    scope = _scope(profile_id)
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

    logger.info("📚 Collection (%s): %d live keyword(s), %d live prompt(s)",
                scope, len(keywords), len(prompts))
    return {"keywords": keywords, "prompts": prompts}


def set_harvest_flag(category: str, days: int, profile_id: str | None = None) -> None:
    """All skills covered — suppress generation for `days` (harvest mode)."""
    key = HARVEST_KEY.format(category=category, scope=_scope(profile_id))
    safe_redis_operation(keyword_broker.set, key, "1", ex=max(1, days) * 86400)


def harvest_active(category: str, profile_id: str | None = None) -> bool:
    """True while the harvest-mode window is open for this profile."""
    key = HARVEST_KEY.format(category=category, scope=_scope(profile_id))
    return safe_redis_operation(keyword_broker.exists, key) == 1
