"""
main.py — the orchestrator: the loop that never asks you for keywords.

One cycle:
  1. GENERATOR (Actor 1) -> 10-15 keywords
  2. SEQUENTIAL scrape+judge over the array (one keyword at a time —
     captcha safety; run_scrape processes its keywords strictly one
     after another in a single browser session)
  3. Array exhausted -> TIMING ASSIGNER (Actor 2) -> relative days ->
     script converts to absolute TTLs -> keywords NOW enter the
     collection (Redis db3)
  4. Collection >= keyword_pool_limit -> COVERAGE REVIEWER (checkpoint c)
     -> exclusion prompt(s); composing keywords DELETED (redis +
     collection), prompts timed by ACTOR 4 (one call per prompt, in
     parallel); unmapped keywords stay untouched
  5. all_covered -> harvest flag (TTL = recheck_after_days) -> one
     harvest round over the alive keywords -> exit

Browser ownership: ONE session for the whole run. Cycles used to
close/reopen the browser between rounds — slow, and relaunching
Chromium every few minutes looks robotic. run_scrape now accepts an
existing context; this loop holds it open and passes it through.

Redis is truth: a crashed run restarts from load_collection() —
searched keywords and prompts carry their TTLs, expired territories
are simply open again.
"""

import logging
from datetime import datetime, timedelta, timezone

from core import (
    KeywordGenerationRequest,
    KeywordStats,
    PoolReviewRequest,
    PromptTimingRequest,
    RunConfig,
)

from modules.checkpoints import judge_ad_relevance
from modules.scraper import browser_session, run_scrape
from modules.storage import record_results

from . import actors
from . import collection as coll

logger = logging.getLogger(__name__)


def _profile_text(about: str) -> str:
    """About-us text handed to Actors 1 & 3 (additional filters go to the judge only)."""
    return about


def _stats_for_round(
    array: list[str],
    ads_by_kw: dict[str, int],
    relevant_by_kw: dict[str, int],
    category: str,
) -> list[KeywordStats]:
    """One KeywordStats per generated keyword — zeros included (evidence is evidence)."""
    now = datetime.now(timezone.utc)
    stats = []
    for kw in array:
        relevant = relevant_by_kw.get(kw, 0)
        stats.append(
            KeywordStats(
                keyword=kw,
                category=category,
                runs_searched=1,
                ads_fetched=ads_by_kw.get(kw, 0),
                relevant_ads=relevant,
                last_relevant_hit_at=now if relevant else None,
                status_hint="alive" if relevant else "new",
            )
        )
    return stats


async def run_orchestrator(
    config: RunConfig,
    headless: bool = False,
    max_cycles: int | None = None,
    run_mode: str = "generate",  # "existing" or "generate"
    run_id: str | None = None,
) -> dict:
    """
    Run the keyword lifecycle.

    run_mode:
      - "existing": search provided keywords ONCE, then STOP (no AI, no cycles)
      - "generate": cycle 1 = provided keywords + the first AI batch,
        every later cycle = one AI batch only (provided keywords are
        searched exactly once, ever). max_cycles bounds COMPLETED cycles.
    run_id: name of the single result file this run writes (see record_results).
    """
    category_id = config.filters.category
    about, additional_filters = _load_texts()
    logger.info("🚀 Orchestrator start — category %r, pool limit %d, mode=%s", category_id, config.keyword_pool_limit, run_mode)

    totals = {"cycles": 0, "keywords_searched": 0, "relevant_ads": 0}
    # Accumulator: every keyword actually searched this run, in order. It is
    # filled in place by _search_and_judge, so every return carries the list.
    searched_keywords: list[str] = []
    totals["searched_keywords"] = searched_keywords

    async with browser_session(headless=headless) as context:
        # ──────────────────────────────────────────────────────────────
        # EXISTING MODE: provided keywords ONCE, then stop (no AI at all)
        # ──────────────────────────────────────────────────────────────
        user_keywords = list(config.filters.keywords or [])

        if run_mode == "existing":
            if user_keywords:
                logger.info("🔍 Searching %d user-provided keyword(s): %s", len(user_keywords), user_keywords)
                relevant, _, _ = await _search_and_judge(
                    user_keywords, config, additional_filters, context, run_id, searched_keywords,
                )
                totals["cycles"] = 1
                totals["keywords_searched"] += len(user_keywords)
                totals["relevant_ads"] += relevant
            logger.info("✅ Existing mode: searched user keywords only — stopping")
            return totals

        if run_mode != "generate":
            return totals  # safety

        # In generate mode the provided keywords ride along in cycle 1 (one
        # cycle, searched once) — never repeated in later cycles.
        provided_pending = bool(user_keywords)

        while True:
            live = coll.load_collection(category_id)
            keywords_live: list[KeywordStats] = live["keywords"]
            prompts_live: list[str] = [p["prompt"] for p in live["prompts"]]

            # ---------------- HARVEST MODE ----------------
            if coll.harvest_active(category_id):
                logger.info("🌾 Harvest mode: re-searching %d alive keyword(s) for fresh ads", len(keywords_live))
                if not keywords_live:
                    return totals
                relevant, _, _ = await _search_and_judge(
                    [s.keyword for s in keywords_live], config, additional_filters, context, run_id, searched_keywords,
                )
                totals["cycles"] += 1
                totals["relevant_ads"] += relevant
                logger.info("🌾 Harvest round done — %d relevant ads this round", relevant)
                return totals

            # ---------------- CYCLE BUDGET ----------------
            # Checked BEFORE generating: max_cycles bounds COMPLETED cycles,
            # so max_cycles=1 still gets the AI batch of cycle 1.
            if max_cycles is not None and totals["cycles"] >= max_cycles:
                logger.info("🛑 Cycle budget reached (%d) — stopping", max_cycles)
                return totals

            # ---------------- AI KEYWORD GENERATION ----------------
            request = KeywordGenerationRequest(
                category_description=_category_description(category_id),
                user_profile_text=_profile_text(about),
                exclusion_prompts=prompts_live,
                searched_keywords=[s.keyword for s in keywords_live],
                target_count=config.keywords_per_generation,
            )
            batch = await actors.generate_keywords(request)
            array = batch.keywords[: config.keywords_per_generation]
            if not array:
                logger.warning("⚠️ Generator returned nothing — stopping (retry later)")
                return totals
            logger.info("🗝️ Cycle %d: generated %d keyword(s)", totals["cycles"] + 1, len(array))

            # ---------------- SEQUENTIAL SEARCH + JUDGE ----------------
            # Cycle 1 searches provided keywords + this batch together.
            search_array = user_keywords + array if provided_pending else array
            provided_pending = False
            relevant, ads_by_kw, relevant_by_kw = await _search_and_judge(
                search_array, config, additional_filters, context, run_id, searched_keywords,
            )
            totals["cycles"] += 1
            totals["keywords_searched"] += len(search_array)
            totals["relevant_ads"] += relevant

            # ---------------- TIMING (Actor 2) ----------------
            # Over the generated array only: the user-provided keywords were
            # never part of the managed collection, exactly as before.
            stats = _stats_for_round(array, ads_by_kw, relevant_by_kw, category_id)
            timings = await actors.assign_keyword_timings(stats)
            for s in stats:
                t = timings.get(s.keyword)
                days = t.reuse_after_days if t and t.reuse_after_days is not None else 1
                coll.save_keyword(s, category_id, datetime.now(timezone.utc) + timedelta(days=days))
                logger.info("⏳ %r -> reuse in %d day(s) (%s)", s.keyword, days, t.reason if t else "default")

            # ---------------- THRESHOLD -> COVERAGE REVIEW ----------------
            live = coll.load_collection(category_id)
            keywords_live = live["keywords"]
            if len(keywords_live) < config.keyword_pool_limit:
                logger.info("📚 Collection: %d/%d — below limit, next cycle", len(keywords_live), config.keyword_pool_limit)
                continue

            logger.info("📚 Collection hit the limit (%d) — coverage review", len(keywords_live))
            review = await actors.review_coverage(
                PoolReviewRequest(
                    user_profile_text=_profile_text(about),
                    keywords=keywords_live,
                    exclusion_prompts=[p["prompt"] for p in live["prompts"]],
                    pool_limit=config.keyword_pool_limit,
                )
            )

            # ---------------- PROMPT TIMING (Actor 4, parallel) ----------------
            stats_by_kw = {s.keyword: s for s in keywords_live}
            timing_requests = [
                PromptTimingRequest(
                    exclusion_prompt=p.prompt,
                    composing_keywords=[stats_by_kw[k] for k in p.covered_keywords if k in stats_by_kw],
                )
                for p in review.prompts
            ]
            timings = (
                await actors.assign_prompt_timings_parallel(timing_requests)
                if timing_requests
                else []
            )

            now = datetime.now(timezone.utc)
            for prompt, timing in zip(review.prompts, timings):
                coll.save_prompt(
                    prompt.prompt, prompt.covered_keywords, category_id,
                    expires_at=now + timedelta(days=timing.expire_after_days),
                )
                coll.delete_keywords(category_id, prompt.covered_keywords)
                logger.info(
                    "🧩 Prompt %r replaces %d keyword(s), expires in %d day(s)",
                    prompt.prompt, len(prompt.covered_keywords), timing.expire_after_days,
                )

            if review.focus_guidance:
                logger.info("🎯 Focus guidance: %s", review.focus_guidance)

            # ---------------- ALL COVERED -> HARVEST ----------------
            if review.all_covered and config.harvest_enabled:
                coll.set_harvest_flag(category_id, review.recheck_after_days or 7)
                logger.info("✅ All skills covered — harvest mode for %d day(s)", review.recheck_after_days or 7)


def _load_texts() -> tuple[str, str]:
    from modules.storage import load_profile

    return load_profile()


def _category_description(category_id: str) -> str:
    from core import CATEGORIES

    return CATEGORIES[category_id].description


async def _search_and_judge(
    keywords: list[str],
    config: RunConfig,
    additional_filters: str,
    context,
    run_id: str | None = None,
    searched_keywords: list[str] | None = None,
) -> tuple[int, dict[str, int], dict[str, int]]:
    """
    One sequential scrape+judge pass over `keywords`, on the SHARED
    browser context (no relaunch between cycles). `run_id` names the single
    result file for the whole run; `searched_keywords` is appended to in place
    so every cycle's keywords end up in the run summary and the file's
    "keywords" list — including keywords that return zero ads.
    Returns (relevant_count, ads_fetched_by_keyword, relevant_by_keyword).
    """
    filters = config.filters.model_copy(update={"keywords": keywords})
    ads_by_kw: dict[str, int] = {kw: 0 for kw in keywords}
    relevant_by_kw: dict[str, int] = {kw: 0 for kw in keywords}

    if searched_keywords is not None:
        searched_keywords.extend(keywords)

    ads = await run_scrape(filters, headless=False, context=context)
    if not ads:
        return 0, ads_by_kw, relevant_by_kw

    for ad in ads:
        if ad.source_keyword in ads_by_kw:
            ads_by_kw[ad.source_keyword] += 1

    verdicts = await judge_ad_relevance(
        ads,
        batch_size=config.judge_batch_size,
        max_concurrent=config.judge_max_concurrent,
        additional_filters=additional_filters or None,
    )
    summary = record_results(
        ads, verdicts, config.filters.category, config.filters,
        run_id=run_id, searched_keywords=searched_keywords,
    )
    for ad in summary["accepted"]:
        if ad.source_keyword in relevant_by_kw:
            relevant_by_kw[ad.source_keyword] += 1
    return len(summary["accepted"]), ads_by_kw, relevant_by_kw
