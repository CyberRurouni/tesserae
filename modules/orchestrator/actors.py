"""
actors.py — the four AI citizens of the keyword lifecycle.

  1. generate_keywords   (Actor 1): category + about + exclusion prompts +
     searched keywords  ->  10-15 new keywords. NO timing here.
  2. assign_keyword_timings (Actor 2): the exhausted array + raw stats ->
     RELATIVE reuse delays (days). Script converts to absolute TTLs.
  3. review_coverage     (checkpoint c): the whole collection + about ->
     exclusion prompt(s) for keyword-groups covering one skill/subskill,
     an explicit all_covered verdict, and focus guidance. Keywords not
     mapped to any prompt are NOT replaced.
  4. assign_prompt_timings (Actor 4): ONE call PER PROMPT (run in
     parallel by the caller) — prompt + its composing keywords' stats ->
     RELATIVE expiry (days).

Rules the prompts encode (agreed design):
  - The AI reasons in RELATIVE durations only; calendar math is the
    script's job.
  - The script never hard-codes keyword quality — raw stats are given,
    the AI weighs them.
  - Fail-closed on AI failure: generation returns [] (the loop retries
    later), timings/coverage raise (caller decides), because a silent
    zero here would corrupt the lifecycle.
"""

import asyncio
import json
import logging

from core import (
    AIClient,
    ExclusionPrompt,
    KeywordBatch,
    KeywordGenerationRequest,
    KeywordStats,
    KeywordTiming,
    KeywordTimingResult,
    PoolReviewRequest,
    PoolReviewResult,
    PromptTiming,
    PromptTimingRequest,
)

logger = logging.getLogger(__name__)


def _json_hint(model_name: str) -> str:
    return f"Answer with JSON only, shaped exactly like: {model_name}"


# ============================================================================
# 🔹 ACTOR 1 — keyword generator
# ============================================================================

_GENERATOR_SYSTEM = """\
You generate Meta Ads Library SEARCH KEYWORDS for a lead-generation system.

You receive:
  - the CATEGORY description (what kind of opportunity the user wants)
  - the USER PROFILE (about the user: skills, projects, preferences)
  - EXCLUSION PROMPTS (compressed territories already covered — do NOT \
regenerate keywords inside these)
  - SEARCHED KEYWORDS (already used, currently cooling down — do NOT \
repeat them; variations that reach DIFFERENT advertisers are welcome)

Rules:
- Return 10-15 keywords, one search phrase each (2-5 words, as a real \
person might search the ads library).
- Each keyword must target UNCOVERED territory: a specific skill, \
subskill, industry or hiring context implied by the user's profile.
- Never return a keyword from the searched list or inside an exclusion \
prompt's territory.
- No timestamps, no explanations in the array itself.

Answer with JSON only:
{"keywords": ["...", "..."], "notes": "<max 25 words on your targeting strategy>"}
"""


async def generate_keywords(request: KeywordGenerationRequest) -> KeywordBatch:
    client = AIClient()
    user_prompt = (
        f"CATEGORY DESCRIPTION:\n{request.category_description}\n\n"
        f"USER PROFILE:\n{request.user_profile_text or '(not provided)'}\n\n"
        f"EXCLUSION PROMPTS (territories already covered — avoid):\n"
        + ("\n".join(f"- {p}" for p in request.exclusion_prompts) or "(none)")
        + "\n\n"
        f"SEARCHED KEYWORDS (already used — do not repeat):\n"
        + ("\n".join(f"- {k}" for k in request.searched_keywords) or "(none)")
        + f"\n\nGenerate {request.target_count} new search keywords "
        f"(10-{max(10, request.target_count)})."
    )
    raw = await client.blocking(
        [
            {"role": "system", "content": _GENERATOR_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=1500,
        temperature=0.7,  # creative divergence is the point here
    )
    batch = KeywordBatch(
        keywords=[k.strip() for k in raw.get("keywords", []) if str(k).strip()],
        notes=raw.get("notes"),
    )
    logger.info("🗝️ Generator: %d keyword(s) — %s", len(batch.keywords), batch.notes or "")
    return batch


# ============================================================================
# 🔹 ACTOR 2 — keyword timing assigner
# ============================================================================

_KW_TIMING_SYSTEM = """\
You assign REUSE DELAYS to search keywords after their scrape round.

For each keyword you get its RAW STATS: how many times it was searched, \
how many ads passed heuristics, how many were RELEVANT (actual \
opportunities the user wants), and when it last produced one.

Rules:
- Base the delay on EVIDENCE, not vibes: high relevant yield -> short \
delay (it pays to come back soon); zero relevant yield across several \
searches -> long delay; enormous ad volume with near-zero relevance -> \
long delay (wrong territory).
- Answer in RELATIVE DAYS ONLY. The system converts to absolute times.
- Delay range: 1 (hot) to 30 (exhausted) days. Every keyword MUST get \
an entry.

Answer with JSON only:
{"timings": [{"keyword": "<exact keyword>", "reuse_after_days": <1-30>, \
"reason": "<max 12 words>"}]}
"""


async def assign_keyword_timings(request_stats: list[KeywordStats]) -> dict[str, KeywordTiming]:
    """Returns {keyword: KeywordTiming} for every input keyword."""
    client = AIClient()
    listing = "\n".join(
        f"- {s.keyword!r}: searched {s.runs_searched}x, ads_fetched {s.ads_fetched}, "
        f"relevant {s.relevant_ads} (last hit: {s.last_relevant_hit_at.date().isoformat() if s.last_relevant_hit_at else 'never'})"
        for s in request_stats
    )
    raw = await client.blocking(
        [
            {"role": "system", "content": _KW_TIMING_SYSTEM},
            {"role": "user", "content": f"{len(request_stats)} keywords:\n{listing}"},
        ],
        max_tokens=2500,
    )
    result = KeywordTimingResult.model_validate(raw)

    timings: dict[str, KeywordTiming] = {}
    for t in result.timings:
        timings[t.keyword.strip().lower()] = t

    # Fail-closed normalization: every input keyword gets a timing; AI
    # silences or renames get the script default (set by the caller via
    # None -> default_cooldown_seconds).
    normalized: dict[str, KeywordTiming] = {}
    for s in request_stats:
        normalized[s.keyword] = timings.get(s.keyword.lower()) or KeywordTiming(
            keyword=s.keyword, reuse_after_days=None, reason="no AI timing — default cooldown"
        )
    logger.info("⏱️ Keyword timings assigned for %d keyword(s)", len(normalized))
    return normalized


# ============================================================================
# 🔹 CHECKPOINT C — coverage reviewer
# ============================================================================

_REVIEWER_SYSTEM = """\
You compress a keyword collection into EXCLUSION PROMPTS and judge coverage.

You receive the user profile, the live keyword collection (with stats) \
and the exclusion prompts already in place.

A GROUP of keywords 'covers' a skill/subskill when together they \
exhaust that territory — nothing meaningful inside it remains \
unsearchable by them.

Tasks:
1. Find keyword groups that TOGETHER cover one skill or subskill of \
the user. For each such group output ONE exclusion prompt: a short \
search-like phrase (2-6 words) that STANDS FOR the whole territory, \
plus the EXACT keywords it replaces (copied verbatim from the input).
2. Keywords that do not fully map into any group are LEFT ALONE — do \
not force them into prompts.
3. Do not rewrite or extend existing prompts; they expire on their own.
4. ALL SKILLS COVERED: only when the collection (keywords + existing \
prompts) leaves no user skill/subskill unexplored, state so \
explicitly AND give recheck_after_days (how many days of harvest \
before generation should reopen). If unsure, say false — the loop \
continues and you will review again later.

Answer with JSON only:
{"prompts": [{"prompt": "...", "covered_keywords": ["exact", "keywords"], \
"disposition": "reuse_soon|cool_down|retire"}],
 "focus_guidance": "<max 20 words: what future keywords should target>",
 "all_covered": true|false,
 "recheck_after_days": <int, required when all_covered>,
 "reasoning": "<max 40 words>"}
"""


async def review_coverage(request: PoolReviewRequest) -> PoolReviewResult:
    client = AIClient()
    kw_listing = "\n".join(
        f"- {s.keyword!r}: searched {s.runs_searched}x, relevant {s.relevant_ads}"
        for s in request.keywords
    ) or "(empty)"
    user_prompt = (
        f"USER PROFILE:\n{request.user_profile_text or '(not provided)'}\n\n"
        f"LIVE KEYWORDS ({len(request.keywords)}):\n{kw_listing}\n\n"
        f"EXISTING EXCLUSION PROMPTS:\n"
        + ("\n".join(f"- {p}" for p in request.exclusion_prompts) or "(none)")
        + f"\n\nCollection size: {len(request.keywords)} (limit {request.pool_limit})."
    )
    raw = await client.blocking(
        [
            {"role": "system", "content": _REVIEWER_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=2500,
    )
    result = PoolReviewResult.model_validate(raw)

    # Enforce the invariant: covered_keywords must exist verbatim in the
    # collection — anything else would delete unknown text.
    known = {s.keyword for s in request.keywords}
    valid: list[ExclusionPrompt] = []
    for p in result.prompts:
        real = [k for k in p.covered_keywords if k in known]
        if not real:
            logger.warning("⚠️ Reviewer proposed a prompt matching no keywords — dropped: %r", p.prompt)
            continue
        valid.append(p.model_copy(update={"covered_keywords": real}))
    result = result.model_copy(update={"prompts": valid})

    logger.info(
        "🧩 Coverage review: %d prompt(s), all_covered=%s — %s",
        len(result.prompts), result.all_covered, (result.reasoning or "")[:80],
    )
    return result


# ============================================================================
# 🔹 ACTOR 4 — prompt timing assigner (one call per prompt, parallel)
# ============================================================================

_PROMPT_TIMING_SYSTEM = """\
You assign an EXPIRY to an exclusion prompt.

The prompt compresses several search keywords into one territory. You \
get the prompt and the RAW STATS of exactly the keywords it replaced.

Rules:
- Rich relevant yield inside the territory -> short expiry (1-3 days: \
new ads appear constantly, worth re-searching soon).
- Thin or empty yield -> long expiry (up to 60 days: nothing grows \
there).
- RELATIVE DAYS ONLY.

Answer with JSON only:
{"expire_after_days": <1-60>, "reason": "<max 12 words>"}
"""


async def assign_prompt_timing(request: PromptTimingRequest) -> PromptTiming:
    client = AIClient()
    listing = "\n".join(
        f"- {s.keyword!r}: searched {s.runs_searched}x, ads_fetched {s.ads_fetched}, relevant {s.relevant_ads}"
        for s in request.composing_keywords
    )
    raw = await client.blocking(
        [
            {"role": "system", "content": _PROMPT_TIMING_SYSTEM},
            {"role": "user", "content": (
                f"EXCLUSION PROMPT: {request.exclusion_prompt!r}\n\n"
                f"Stats of the keywords it replaces:\n{listing}"
            )},
        ],
        max_tokens=300,
    )
    timing = PromptTiming.model_validate(raw)
    logger.info("⏱️ Prompt timing: %r expires in %d day(s)", request.exclusion_prompt, timing.expire_after_days)
    return timing


async def assign_prompt_timings_parallel(
    requests: list[PromptTimingRequest],
    max_concurrent: int = 5,
) -> list[PromptTiming]:
    """All per-prompt timing calls run concurrently (≤ max_concurrent)."""
    gate = asyncio.Semaphore(max_concurrent)

    async def _one(req: PromptTimingRequest) -> PromptTiming:
        async with gate:
            return await assign_prompt_timing(req)

    return list(await asyncio.gather(*(_one(r) for r in requests)))


# Convenience re-export for callers that want the json import used.
_ = json
