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

import inspect
import logging
from datetime import datetime, timedelta, timezone

from core import (
    AdRecord,
    AdRelevanceVerdict,
    KeywordGenerationRequest,
    KeywordStats,
    PoolReviewRequest,
    PromptTimingRequest,
    RunConfig,
    UserProfile,
)

from modules.judge import judge_ad_relevance
from modules.judge.judge import judge_request_relevance
from modules.profiles import held as held_store
from modules.scraper import browser_session, run_scrape
from modules.scraper.seen import RunSeenIds
from modules.storage import record_results, resolve_lens_state

from . import actors
from . import collection as coll

logger = logging.getLogger(__name__)


def _profile_text(about: str) -> str:
    """About-us text handed to Actors 1 & 3 (additional filters go to the judge only)."""
    return about


async def _regate_held(
    config: RunConfig,
    category_id: str,
    request_text: str,
    profile_threshold: float,
    request_threshold: float,
    *,
    run_id: str | None,
    searched_keywords: list[str],
) -> None:
    """
    Reconsider held ads after a threshold was lowered.

    A threshold at or above the one the ad already missed means it stays parked
    — that is the skip, and it costs no AI. A lower threshold re-gates the
    stored confidence: request-stage holds can be promoted locally, while a
    profile-stage hold needs exactly one request-judge call because its profile
    answer has not changed.
    """
    profile_id = config.profile_id
    if not profile_id:
        return

    records = held_store.load(profile_id)
    if not records:
        return

    direct_ads: list[AdRecord] = []
    direct_verdicts: dict[str, AdRelevanceVerdict] = {}
    request_candidates: list[AdRecord] = []
    updated = skipped = 0

    for record in records:
        stage = record.get("stage")
        if stage == held_store.HELD_STAGE_REQUEST:
            if not config.request_id or record.get("request_id") != config.request_id:
                continue
            threshold = request_threshold
        elif stage == held_store.HELD_STAGE_PROFILE:
            threshold = profile_threshold
        else:
            continue

        if held_store.skip_for(record, profile_threshold, request_threshold):
            skipped += 1
            continue

        ad_id = record.get("ad_archive_id")
        if not ad_id:
            continue
        if not held_store.clears(record, threshold):
            held_store.update_threshold(
                profile_id, ad_id, threshold,
                stage=stage,
                request_id=record.get("request_id"),
            )
            updated += 1
            continue

        raw_ad = record.get("ad") or {}
        try:
            ad = AdRecord(**raw_ad)
        except Exception as exc:  # noqa: BLE001 - a corrupt hold must not stop a run
            logger.warning("⚠️ Held ad %s has an unreadable record (%s) — skipping it",
                           ad_id, exc)
            continue

        if stage == held_store.HELD_STAGE_REQUEST or not request_text.strip():
            direct_ads.append(ad)
            direct_verdicts[ad_id] = AdRelevanceVerdict(
                ad_archive_id=ad_id,
                outcome="accepted",
                confidence=float(record.get("confidence") or 1.0),
                reason=str(record.get("reason") or "promoted after a lower threshold"),
                ad_category=str(record.get("ad_category") or ""),
                request_id=config.request_id,
                stage=stage,
            )
        else:
            request_candidates.append(ad)

    if direct_ads:
        summary = record_results(
            direct_ads, direct_verdicts, category_id, config.filters,
            run_id=run_id, searched_keywords=searched_keywords,
            profile_id=profile_id, request_id=config.request_id,
            profile_threshold=profile_threshold,
            request_threshold=request_threshold,
        )
        logger.info(
            "🤔 Re-gated %d held ad(s): %d promoted without AI",
            len(direct_ads), len(summary.get("accepted") or []),
        )

    if request_candidates:
        verdicts = await judge_request_relevance(
            request_candidates,
            request_text,
            request_id=config.request_id,
            request_threshold=request_threshold,
        )
        # The request judge does not return a category. The held record already
        # has the profile judge's label; keep it rather than overwriting it with
        # an empty string when this ad is accepted or held again.
        categories = {
            record.get("ad_archive_id"): record.get("ad_category", "")
            for record in records
        }
        for ad_id, verdict in list(verdicts.items()):
            if not verdict.ad_category and categories.get(ad_id):
                verdicts[ad_id] = verdict.model_copy(update={
                    "ad_category": categories[ad_id],
                })
        summary = record_results(
            request_candidates, verdicts, category_id, config.filters,
            run_id=run_id, searched_keywords=searched_keywords,
            profile_id=profile_id, request_id=config.request_id,
            profile_threshold=profile_threshold,
            request_threshold=request_threshold,
        )
        logger.info(
            "🤔 Re-gated %d profile-held ad(s): %d accepted, %d still held, "
            "%d deferred",
            len(request_candidates), len(summary.get("accepted") or []),
            len(summary.get("held") or []), len(summary.get("deferred") or []),
        )

    if skipped or updated:
        logger.info(
            "🤔 Held ads checked: %d still above the new threshold, %d updated "
            "because confidence still missed it", skipped, updated,
        )


def _stats_for_round(
    array: list[str],
    ads_by_kw: dict[str, int],
    relevant_by_kw: dict[str, int],
    category: str,
    searched_at: datetime | None = None,
) -> list[KeywordStats]:
    """
    One KeywordStats per keyword searched this cycle — zeros included.

    Every keyword in a cycle carries the SAME `searched_at` instant, which is
    what lets `collection.last_batch` identify the batch exactly: the newest
    stamp is the last batch and everything sharing it belongs to it.

    Each record describes ONE search. Accumulating it into the keyword's
    lifetime is the collection's job (`merge_with_history` / `save_keyword`),
    so there is exactly one place where numbers get added together.
    """
    now = searched_at or datetime.now(timezone.utc)
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
                last_searched_at=now,
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
    profile_id: str | None = None,
    request_id: str | None = None,
    profile_threshold: float | None = None,
    request_threshold: float | None = None,
    on_progress=None,
    should_stop=None,
    on_step=None,
) -> dict:
    """
    Run the keyword lifecycle.

    `should_stop` is polled between keywords, between cycles, and every scroll
    step inside a keyword (see modules/scraper). It always unwinds by returning,
    never by raising, so a stopped run is indistinguishable from a finished one
    except for its status — which is what makes it safe to expose a Stop button.
    A stopped keyword still has its collected ads judged and saved.
    `on_step` reports per-scroll liveness.

    run_mode:
      - "existing": search provided keywords ONCE, then STOP (no AI, no cycles)
      - "generate": cycle 1 = provided keywords + the first AI batch,
        every later cycle = one AI batch only (provided keywords are
        searched exactly once, ever). max_cycles bounds COMPLETED cycles.
    run_id: name of the single result file this run writes (see record_results).
    """
    if request_id and not config.request_id:
        config = config.model_copy(update={"request_id": request_id})

    category_id = config.filters.category

    # ONE lens for the whole run, resolved once from the profile store.
    #
    # The keyword generator and both relevance judges read the same resolved
    # object, so a run cannot hunt for one person and grade the results for
    # another. Thresholds ride with the request and are snapshotted here; the
    # API may pass the exact values it displayed so a mid-run save cannot move
    # the goalposts underneath an active run.
    lens = resolve_lens_state(config.profile_id, config.request_id)
    about = lens.profile_text
    additional_filters = lens.request_text
    profile_threshold = (
        lens.profile_threshold if profile_threshold is None else profile_threshold
    )
    request_threshold = (
        lens.request_threshold if request_threshold is None else request_threshold
    )
    config = config.model_copy(update={
        "profile": UserProfile(background=about),
        "profile_id": lens.profile_id,
        "request_id": lens.request_id,
    })
    if not about.strip():
        logger.warning(
            "⚠️ No profile text resolved — the AI actors have nothing to judge against. "
            "Write your profile on the 'About you' page."
        )
    logger.info(
        "🔎 Lens resolved: profile=%s request=%s (%d chars of profile text), "
        "strictness profile=%.2f request=%.2f",
        config.profile_id or "(active)",
        config.request_id or "(none)",
        len(about), profile_threshold, request_threshold,
    )

    logger.info(
        "🚀 Orchestrator start — category %r, pool limit %d, mode=%s, profile=%s, workers=%d",
        category_id, config.keyword_pool_limit, run_mode,
        config.profile_id or "(no profile)", config.parallel_workers,
    )

    totals = {"cycles": 0, "keywords_searched": 0, "relevant_ads": 0}

    # One run-scoped set of harvested ids, shared by every cycle and every
    # worker on this run. Without a run id it degrades to memory-only.
    run_seen = RunSeenIds(run_id)
    # Accumulator: every keyword actually searched this run, in order. It is
    # filled in place by _search_and_judge, so every return carries the list.
    searched_keywords: list[str] = []
    totals["searched_keywords"] = searched_keywords

    # Lowering a threshold is supposed to reconsider ads already held. Do that
    # before scraping, so the run can report promotions even when every keyword
    # is skipped: profile-held ads need one request call, request-held ads need
    # none at all.
    await _regate_held(
        config, category_id, additional_filters,
        profile_threshold, request_threshold,
        run_id=run_id, searched_keywords=searched_keywords,
    )

    async with browser_session(headless=headless) as context:
        # ──────────────────────────────────────────────────────────────
        # EXISTING MODE: provided keywords ONCE, then stop (no AI at all)
        # ──────────────────────────────────────────────────────────────
        user_keywords = list(config.filters.keywords or [])

        if run_mode == "existing":
            if user_keywords:
                logger.info("🔍 Searching %d user-provided keyword(s): %s", len(user_keywords), user_keywords)
                relevant, _, _ = await _search_and_judge(
                    user_keywords, config, additional_filters, context, run_id,
                    searched_keywords, run_seen, on_progress,
                    should_stop=should_stop, on_step=on_step,
                    profile_threshold=profile_threshold,
                    request_threshold=request_threshold,
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
            if should_stop is not None and should_stop():
                logger.info("🛑 Stopped before the next cycle — %d cycle(s), %d keyword(s), %d lead(s)",
                             totals["cycles"], totals["keywords_searched"], totals["relevant_ads"])
                return totals

            live = coll.load_collection(category_id, config.profile_id)
            # Everything we have ever searched — the generator's memory, and the
            # pool the keyword leaderboard is ranked from.
            keywords_known: list[KeywordStats] = live["keywords"]
            # The subset whose cooldown has elapsed: the live pool. Harvest
            # re-searches this, and the coverage reviewer sizes itself on it.
            keywords_live: list[KeywordStats] = live["searchable"]
            prompts_live: list[str] = [p["prompt"] for p in live["prompts"]]

            # ---------------- HARVEST MODE ----------------
            if coll.harvest_active(category_id, config.profile_id):
                pool = keywords_live
                logger.info("🌾 Harvest mode: %d searchable keyword(s) in the pool", len(pool))

                # The user picks what gets re-searched: either a number ("top
                # 20") or the keywords themselves. Harvest exists to catch ads
                # posted SINCE the last pass, so the keywords worth a second
                # pass are the ones that earned leads — ranked, not arbitrary.
                harvested, notes = coll.resolve_harvest(
                    pool, config.harvest_limit, config.harvest_keywords,
                )
                for note in notes:
                    logger.warning("⚠️  Harvest: %s", note)

                # Harvest re-searches what it already has. Keywords the user
                # typed for THIS run are not in that collection, so silently
                # replacing them meant typing a keyword and watching it never
                # be searched — with no error, and harvest mode is silent
                # enough that nothing on screen suggests why.
                round_keywords = list(harvested)
                if provided_pending and user_keywords:
                    round_keywords = user_keywords + round_keywords
                    provided_pending = False
                    logger.info(
                        "🌾 Harvest mode: %d keyword(s) from this run ride along first",
                        len(user_keywords),
                    )
                if not round_keywords:
                    logger.info("🌾 Harvest mode: nothing selected to re-search — stopping")
                    return totals
                relevant, _, _ = await _search_and_judge(
                    round_keywords, config, additional_filters, context, run_id,
                    searched_keywords, run_seen, on_progress,
                    should_stop=should_stop, on_step=on_step,
                    profile_threshold=profile_threshold,
                    request_threshold=request_threshold,
                )
                totals["cycles"] += 1
                totals["keywords_searched"] += len(round_keywords)
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
                # Full history WITH what each keyword actually returned. A bare
                # list of names told the generator only what to avoid; the stats
                # tell it which shape reached advertisers, which is the only
                # thing that makes its next batch better than its last.
                searched_keywords=keywords_known,
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
                search_array, config, additional_filters, context, run_id,
                searched_keywords, run_seen, on_progress,
                should_stop=should_stop, on_step=on_step,
                profile_threshold=profile_threshold,
                request_threshold=request_threshold,
            )
            totals["cycles"] += 1
            totals["keywords_searched"] += len(search_array)
            totals["relevant_ads"] += relevant

            # ---------------- TIMING (Actor 2) ----------------
            # Over EVERY keyword searched this cycle, the user's own included.
            # The typed ones used to be left out, which meant the keywords that
            # actually work best — the ones a person thought of — were invisible
            # to both the cooldown and the leaderboard the generator learns from.
            stats = _stats_for_round(
                search_array, ads_by_kw, relevant_by_kw, category_id,
                searched_at=datetime.now(timezone.utc),
            )
            # Actor 2 weighs a keyword's long-term value from how many times it
            # has been searched. It was always handed exactly one search, so
            # fold in the history before it decides anything.
            stats = coll.merge_with_history(stats, category_id, config.profile_id)
            timings = await actors.assign_keyword_timings(stats)
            for s in stats:
                t = timings.get(s.keyword)
                days = t.reuse_after_days if t and t.reuse_after_days is not None else 1
                saved = coll.save_keyword(
                    s, category_id,
                    datetime.now(timezone.utc) + timedelta(days=days),
                    profile_id=config.profile_id,
                )
                logger.info(
                    "⏳ %r -> reuse in %d day(s) (%s) · lifetime: %d search(es), %d ads, %d lead(s)",
                    s.keyword, days, t.reason if t else "default",
                    saved.runs_searched, saved.ads_fetched, saved.relevant_ads,
                )

            # A stopped run has already had its keyword timings written, so the
            # evidence from this partial run is kept. It stops HERE rather than
            # compressing the pool or generating another batch — the point of
            # pressing stop is that nothing more happens.
            if should_stop is not None and should_stop():
                logger.info(
                    "🛑 Run stopped — %d cycle(s), %d keyword(s) searched, %d lead(s). "
                    "Everything collected has been judged and saved.",
                    totals["cycles"], totals["keywords_searched"], totals["relevant_ads"],
                )
                return totals

            # ---------------- THRESHOLD -> COVERAGE REVIEW ----------------
            live = coll.load_collection(category_id, config.profile_id)
            keywords_live = live["searchable"]
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
                    profile_id=config.profile_id,
                )
                coll.delete_keywords(category_id, prompt.covered_keywords, config.profile_id)
                logger.info(
                    "🧩 Prompt %r replaces %d keyword(s), expires in %d day(s)",
                    prompt.prompt, len(prompt.covered_keywords), timing.expire_after_days,
                )

            if review.focus_guidance:
                logger.info("🎯 Focus guidance: %s", review.focus_guidance)

            # ---------------- ALL COVERED -> HARVEST ----------------
            if review.all_covered and config.harvest_enabled:
                coll.set_harvest_flag(category_id, review.recheck_after_days or 7, config.profile_id)
                logger.info("✅ All skills covered — harvest mode for %d day(s)", review.recheck_after_days or 7)



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
    run_seen: RunSeenIds | None = None,
    on_progress=None,
    should_stop=None,
    on_step=None,
    profile_threshold: float = 0.5,
    request_threshold: float = 0.5,
) -> tuple[int, dict[str, int], dict[str, int]]:
    """
    Scrape and judge `keywords`, judging and saving each keyword as it lands.

    A single pass on the SHARED browser context (no relaunch between cycles).
    `run_id` names the single result file for the whole run;
    `searched_keywords` is appended to in place so every cycle's keywords end
    up in the run summary and the file's "keywords" list — including keywords
    that return zero ads.

    Judging per keyword rather than per pass is what makes a long run show
    anything at all. Scraping a keyword with ~570 results takes minutes, so
    batching every keyword into one judgement left the UI empty for the whole
    crawl, and any failure after the crawl discarded every ad harvested before
    it. `on_progress` reports the harvest as it happens so the run can be seen
    to be alive, and each keyword's verdicts are written to the run file the
    moment they land.
    Returns (relevant_count, ads_fetched_by_keyword, relevant_by_keyword).
    """
    filters = config.filters.model_copy(update={"keywords": keywords})
    ads_by_kw: dict[str, int] = {kw: 0 for kw in keywords}
    relevant_by_kw: dict[str, int] = {kw: 0 for kw in keywords}

    if searched_keywords is not None:
        searched_keywords.extend(keywords)

    judged_total = 0

    async def judge_batch(ads: list, keyword: str) -> int:
        nonlocal judged_total
        for ad in ads:
            if ad.source_keyword in ads_by_kw:
                ads_by_kw[ad.source_keyword] += 1
        if not ads:
            return 0

        # Judging is long and used to be mute. A keyword can now send hundreds
        # of ads to the model, and the crawl's per-scroll tick stops the moment
        # scrolling stops — so a run doing exactly the right thing looked
        # identical to a run that had hung. Ticking per batch is what lets the
        # idle detector tell those apart, and it doubles as the "judging 120 of
        # 480" line on the screen.
        _judged_done = 0

        async def _tick(batch_ads: int) -> None:
            nonlocal _judged_done
            _judged_done += batch_ads
            if on_progress is None:
                return
            try:
                on_progress(
                    phase="judging",
                    keyword=keyword,
                    judged=_judged_done,
                    judging_total=len(ads),
                )
            except Exception:  # noqa: BLE001 - progress must never break judging
                logger.debug("⚠️ judging progress tick failed", exc_info=True)

        verdicts = await judge_ad_relevance(
            ads,
            batch_size=config.judge_batch_size,
            max_concurrent=config.judge_max_concurrent,
            additional_filters=additional_filters or None,
            # The judge reasons in two stages and needs BOTH: the profile
            # decides accept-vs-reject, the request only decides
            # accept-vs-defer. Without the profile every off-request ad would
            # look like trash instead of something worth keeping.
            #
            # This is config.profile.background — the SAME field the keyword
            # generator was handed, resolved once from the profile store at the
            # top of the run. It used to be a second, independent read of a text
            # file, which is how the generator and the judge came to describe
            # two different people.
            profile_text=config.profile.background,
            request_id=config.request_id,
            profile_threshold=profile_threshold,
            request_threshold=request_threshold,
            on_batch=_tick,
        )
        summary = record_results(
            ads, verdicts, config.filters.category, config.filters,
            run_id=run_id, searched_keywords=searched_keywords,
            profile_id=config.profile_id, request_id=config.request_id,
            profile_threshold=profile_threshold,
            request_threshold=request_threshold,
        )
        for ad in summary["accepted"]:
            if ad.source_keyword in relevant_by_kw:
                relevant_by_kw[ad.source_keyword] += 1
        judged_total += len(summary["accepted"])
        # The rows, not just the count. The run file is written once at the end,
        # so this is what gets the judged ads to the screen while the run goes on.
        return summary

    async def harvested(batch, keyword, total_unique):
        if on_progress:
            try:
                result = on_progress(phase="harvested", keyword=keyword,
                                     harvested=total_unique, batch_size=len(batch))
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - progress must never break a run
                logger.warning("⚠️ Progress reporting failed (%s) — continuing", exc)
        summary = await judge_batch(batch, keyword)
        # Pass the judged rows up so the page can show them NOW.
        #
        # The run file is written once, when the run ends — it is a record of a
        # finished run, not the live copy. So while the run is going, this is the
        # only route those rows have to the screen, and without it the tiles and
        # the accepted-ads table would read zero for the entire run and then
        # suddenly fill in at the end.
        if on_progress and summary:
            try:
                # Accepted ads come back as records; the rest are already the
                # plain dicts the file stores. Both are flattened here so the
                # page gets exactly the shape the finished file will have.
                rows = {
                    "accepted": summary.get("accepted_entries") or [],
                    "held": summary.get("held") or [],
                    "deferred": summary.get("deferred") or [],
                    "judged_rejected": summary.get("judged_rejects") or [],
                    "ai_failed": summary.get("failed_ads") or [],
                }
                result = on_progress(phase="judged", keyword=keyword,
                                     batch_size=len(batch), rows=rows)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - progress must never break a run
                logger.warning("⚠️ Judged-row reporting failed (%s) — continuing", exc)
        return summary

    ads = await run_scrape(
        filters,
        headless=False,
        context=context,
        run_seen_ids=run_seen,
        profile_id=config.profile_id,
        # Both were omitted, so every run scraped sequentially regardless of
        # the setting: run_scrape defaults parallel_workers to 1, and without
        # a request_id the deferred bucket is never loaded — parked ads get
        # judged again every run.
        request_id=config.request_id,
        parallel_workers=config.parallel_workers,
        on_harvested=harvested if on_progress is not None else None,
        should_stop=should_stop,
        on_step=on_step,
        profile_threshold=profile_threshold,
        request_threshold=request_threshold,
    )

    # Keywords that produced nothing are still counted as searched, and any ads
    # the harvest callback did not already judge (no progress callback, or a
    # reporting hook that failed) are judged here so nothing is silently lost.
    if ads and not on_progress:
        await judge_batch(ads, "(unreported)")
    elif ads:
        seen_in_callback = ads_by_kw and sum(ads_by_kw.values())
        if ads and not seen_in_callback:
            await judge_batch(ads, "(unreported)")

    return judged_total, ads_by_kw, relevant_by_kw
