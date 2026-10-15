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

from .collection import FEEDBACK_HISTORY_LIMIT, last_batch, rank_for_harvest

logger = logging.getLogger(__name__)


def _json_hint(model_name: str) -> str:
    return f"Answer with JSON only, shaped exactly like: {model_name}"


# ============================================================================
# 🔹 ACTOR 1 — keyword generator
# ============================================================================

_GENERATOR_SYSTEM = """\
You write Meta Ads Library search keywords for a lead-generation system.

HOW META'S SEARCH WORKS — this decides everything:
Meta ANDs every word you use. "python backend developer" means the ad must
contain python AND backend AND developer. Every extra word narrows the result
set, so it must earn its place: add a qualifier only when ads commonly pair it
with that phrase. "python developer hiring" works because ads say exactly that.
"python backend developer urgent" fails because almost no ad uses "backend"
and "urgent" together.

WRITE LIKE AN ADVERTISER, NOT A JOB SEEKER.
The keywords that return results are the ones an employer puts in their own
ad copy and targeting — the words they use to win reach, because an advertiser
wants their ad seen. A job seeker types "python backend hiring urgent". No
employer writes that. Job-seeker phrasing is the single reason this search
used to come back empty.

Rules:
- 10-15 keywords, each 2-5 words, as a plain noun phrase naming a role, a
  skill or a stack.
- Name what the person DOES: the technology, the craft, the role.
- Prefer the phrase an advertiser would actually use. "fastapi developer"
  beats "fastapi backend engineer" because the second asks for two things ads
  rarely advertise together.
- No timestamps, no locations.

VERIFIED to return results — Meta's own counts for the last month:
  python developer ............ ~1,100 results
  javascript developer ........ ~780 results
  ai automation specialist ..... hundreds
  backend developer ........... hundreds

VERIFIED to return NOTHING — every one of these matched zero ads:
  python backend consultant wanted
  fastapi engineer contract role
  web scraping project hiring
  python scripting job vacancy
  fastapi backend developer opening

Do not chase volume for its own sake. A broad keyword is only useful if the ads
are the right KIND — name the skill and let the judge decide the rest.

You also receive:
- EXCLUSION PROMPTS — territories already covered. Do not generate keywords
  inside them.
- YOUR MOST RECENT BATCH — what you wrote last time and what it returned. Read
  it against YOUR PROVEN KEYWORDS below: if the batch drifted away from that
  shape, the results will show it. Say in `notes` what you changed as a result.
- EVERYTHING TRIED SO FAR — every keyword you have written, with what it found.
  Zero ads means that exact phrase matched nothing: do not repeat it, and do
  not produce a variation of it. Ads but no leads means the search worked and
  the leads were declined, which is a different problem and a different reason
  to move on.
- YOUR PROVEN KEYWORDS — the ones that found leads for this person. Write
  variations of THEIR shape.

Answer with JSON only:
{"keywords": ["...", "..."], "notes": "<max 25 words on your targeting strategy>"}
"""

# How many proven keywords to show as exemplars.
PROVEN_KEYWORD_LIMIT = 5


def _one_outcome(stats: KeywordStats) -> str:
    """One line of honest feedback: what this keyword actually returned."""
    lifetime = "over %d searches" % stats.runs_searched if stats.runs_searched > 1 else "last search"
    if stats.relevant_ads:
        return f"{stats.ads_fetched} ads, {stats.relevant_ads} lead(s) {lifetime}"
    if stats.ads_fetched:
        return f"{stats.ads_fetched} ads, none wanted {lifetime}"
    return "0 ads — phrase too narrow"


def _feedback_blocks(
    stats: list[KeywordStats],
    batch: list[KeywordStats] | None = None,
) -> str:
    """
    What the generator is told about the keywords already tried.

    Three views, because they answer three different questions:

      most recent batch — where did my last attempt land?
      proven keywords  — where should it land?
      everything tried — what have I already learned?

    The first two are shown together deliberately. A leaderboard alone cannot
    show drift; putting the latest batch beside the exemplars lets the model
    notice it is writing phrases that never match and correct itself, which is
    the one thing a list of past mistakes cannot tell it.

    Returns "" when there is nothing to report, which is the normal state on a
    first run and after a reset. The prompt degrades to its original shape
    rather than carrying empty headings — an unused feedback section is just
    noise the model has to reason past.
    """
    if not stats:
        return ""

    if batch is None:
        batch = last_batch(stats)

    ranked = rank_for_harvest(stats)
    proven = [s for s in ranked if s.relevant_ads > 0][:PROVEN_KEYWORD_LIMIT]
    shown = ranked[:FEEDBACK_HISTORY_LIMIT]

    blocks = []

    if batch:
        lines = "\n".join(f'- "{s.keyword}" — {_one_outcome(s)}' for s in batch)
        blocks.append(
            "YOUR MOST RECENT BATCH — what you wrote last time, and what it "
            "returned. Compare it against YOUR PROVEN KEYWORDS below:\n" + lines
        )

    if proven:
        winners = "\n".join(f'- "{s.keyword}"' for s in proven)
        blocks.append(
            "YOUR PROVEN KEYWORDS — these reached real advertisers and produced "
            "leads for this person. Write variations of THEIR shape, not "
            "repetitions of the words:\n" + winners
        )
    else:
        blocks.append(
            "YOUR PROVEN KEYWORDS: none yet — no keyword tried so far has produced "
            "a lead. Nothing here is a shape worth copying."
        )

    history = "\n".join(f'- "{s.keyword}" — {_one_outcome(s)}' for s in shown)
    remainder = len(ranked) - len(shown)
    if remainder > 0:
        history += f"\n({remainder} older keyword(s) omitted — all weaker than the ones above)"
    blocks.append(
        "EVERYTHING TRIED SO FAR (do not repeat these, and do not produce "
        "variations of the ones that found nothing):\n" + history
    )

    return "\n\n" + "\n\n".join(blocks)


async def generate_keywords(request: KeywordGenerationRequest) -> KeywordBatch:
    client = AIClient()
    feedback = _feedback_blocks(request.searched_keywords)
    user_prompt = (
        f"CATEGORY DESCRIPTION:\n{request.category_description}\n\n"
        f"USER PROFILE:\n{request.user_profile_text or '(not provided)'}\n\n"
        f"EXCLUSION PROMPTS (territories already covered — avoid):\n"
        + ("\n".join(f"- {p}" for p in request.exclusion_prompts) or "(none)")
        + "\n\n"
        + (feedback or "SEARCHED KEYWORDS: none yet — this is the first batch.")
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

For each keyword you get its RAW STATS: how many times it has been searched, \
how many ads the crawler harvested, how many of those were actually \
RELEVANT (real opportunities the user wants), and when it last produced one.

These two numbers are different questions and must not be confused:
  ads_fetched  — ads the crawler saw. Being non-zero means the keyword DOES \
                 surface ads, whatever became of them afterwards.
  relevant_ads — ads that were wanted. This is the one that earns a short \
                 delay, because it is the only evidence the keyword is worth \
                 coming back for.

Rules:
- A keyword with ads_fetched > 0 and relevant_ads == 0 has produced ads that \
were then turned down. That is NOT a dead keyword. Do not describe it as \
returning nothing, and do not park it for the maximum delay — it is a keyword \
whose audience overlaps with the profile but whose results the user declined, \
which is exactly what a later profile or a later request might want. Give it a \
middle delay (roughly 5-12 days).
- Only a keyword with ads_fetched == 0 across REPEATED searches is genuinely \
exhausted, and only then does a long delay follow.
- One search is not a trend. runs_searched == 1 is a single data point; do not \
draw a conclusion about the keyword's long-term value from it.
- Never state "zero results" or "no results" for a keyword whose ads_fetched \
is greater than zero. Describe what actually happened to the ads instead.

Answer in RELATIVE DAYS ONLY. The system converts to absolute times.
Delay range: 1 (hot) to 30 (exhausted) days. Every keyword MUST get an entry.

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
