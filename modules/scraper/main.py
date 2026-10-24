"""
main.py — the citizens of the scraper city.

One job: turn a RunConfig's filters into fresh, deduplicated AdRecords.
Everything heavier (browser opening, filter clicks, card extraction) is
packaged in this city's warehouses (browser.py / search.py / extract.py).
"""

import asyncio
import inspect
import logging
import re
import unicodedata
from functools import lru_cache

from playwright.async_api import BrowserContext, Page

from core import AdRecord, CATEGORIES, ScrapingFilters


from .browser import browser_session
from .extract import extract_ads_from_page
from modules.verdicts import FilterIndex as get_filter_index

from .seen import RunSeenIds
from .search import (
    SCROLL_DISTANCE_PX,
    apply_filters,
    collect_page_html,
    empty_results,
    more_coming,
    read_results_estimate,
    restore_scroll_depth,
    scroll_depth,
    scroll_step,
    search_keyword,
    wait_for_more_ads,
)

logger = logging.getLogger(__name__)


def renew_deferral(profile_id: str | None, ad_id: str) -> bool:
    """
    Extend a deferred ad's TTL when we pass over it.

    A deferred ad met again under the SAME request is not re-judged — the
    request has not changed, so nothing about it has changed. Renewing here is
    what keeps it alive in the not-now bucket instead of quietly ageing out.
    Imported lazily so the scraper city does not depend on the profile package
    at import time.
    """
    if not profile_id:
        return False
    from modules.profiles.deferred import renew

    return renew(profile_id, ad_id)


# Scroll+extract loop tuning, driven by Meta's own '~N results' estimate.
#
# Only two things end a keyword, and both of them require the page to stop
# giving us cards. Nothing the crawler decides on its own ends one.
#
#   NORMAL — the target was reached AND the page then went quiet for
#            QUIET_STEPS_AFTER_TARGET consecutive scrolls. Reaching the target
#            is deliberately NOT a stopping point: the target is a guess at
#            where the bottom is, and only the page can confirm where the
#            bottom actually is. Hitting the number and stopping there is how
#            a keyword that promised 500 results returned 60.
#
#   SHORT  — the page went quiet for QUIET_STEPS_BEFORE_TARGET consecutive
#            scrolls before the target. That is either a network hiccup or Meta
#            stalling, so the budget is longer than a healthy finish gets. If
#            it is still dead at the end we stop anyway — but we log it as a
#            CRAWL FAILURE rather than as an exhausted keyword, because those
#            two mean completely different things to someone reading the log.
#
# Progress is counted in CARDS LAID EYES ON while scrolling this keyword: every
# distinct ad id that appeared on screen, whether or not we had already met it
# under another keyword, and whether or not it passed any filter. It answers
# "how far down this list did we get", not "how much new material did we
# collect". It must not be the run-wide unique count — that made a keyword
# promising 2 results look finished the moment the run had seen 20 ads
# anywhere, so it quit after a single scroll and reported it as searched.
#
#   TARGET_UNIQUE_RATIO — the safety margin, and ONLY that. 0.70 means "once
#     70% of what Meta estimated is on screen, be slightly less patient about
#     confirming the tail". It is not a cap and never was: it cannot end a crawl,
#     because the only exit is the dead-end budget below. Lowering it makes the
#     shorter tail budget kick in SOONER, so 70% is marginally less thorough
#     than 80%, not more — the crawl still walks to the real bottom either way.
#     Measured: 'rust developer' reads ~190 and yields 174 (92%) on its own.
#
# There is deliberately NO step cap and no scroll-distance ceiling. A crawl
# ends when the page stops giving us cards, never because a counter ran out —
# and repeats bank nothing, so a page that has gone dry always reaches its
# dead-end budget on its own. What IS bounded is the stuck-pane recovery below:
# reloading a tab that is already broken just produces another broken tab.
TARGET_UNIQUE_RATIO = 0.70
QUIET_STEPS_BEFORE_TARGET = 5
QUIET_STEPS_AFTER_TARGET = 3

# Consecutive scroll steps in which the results pane refused to move at all.
# Unlike a quiet step this is a fault, not a conclusion, so it does not count
# toward the quiet budget — it triggers a reload instead.
STUCK_SCROLL_LIMIT = 3

# How many times one keyword may reload a stuck tab before we conclude the tab
# itself is unusable. Without this the two rules above cancel each other out:
# every stuck scroll eventually reloads, and every reload resets the quiet
# budget, so a pane that never recovers would loop forever. Giving up here is
# a FAULT exit, not an exhaustion exit, and it says so.
MAX_STUCK_RELOADS = 3

# ── Streaming: two thresholds, both 20, and they are NOT the same number ────
#
# Judging used to happen once per keyword, after that keyword's crawl finished.
# So a keyword returning 549 ads held all 549 in memory, showed nothing at all
# for the whole keyword, and lost every one of them if the run died near the end.
# The fix is to keep two buffers and deal with each on its own cadence:
#
#   FETCH_BATCH — ads read off the page before the cheap filters run.
#   JUDGE_BATCH — ads that SURVIVED those filters before the AI is asked.
#
# They drift apart on purpose. Read 20 and keep 6: the fetch buffer empties and
# the judge buffer gains 6. Read 20 more and keep 11: the judge buffer is at 17,
# still under, so nothing is spent. Sending 20 fetched ads to the AI would mean
# paying to judge ads the heuristics had already thrown away.
#
# Both flush the WHOLE buffer rather than trimming it to exactly the threshold,
# so there is never a remainder to remember. When the judge buffer crosses 20 it
# goes up with whatever else is in there (20-39 ads).
FETCH_BATCH = 20
JUDGE_BATCH = 20


# Typography that carries no meaning for matching, folded to a plain form.
# Apostrophes first: Meta's ad copy uses the curly ones, so "we\u2019re hiring" was
# not matching the pattern "we're hiring" — one of the category's own phrases had
# never matched anything. Dashes are included for the same reason.
_CHAR_FOLDING = (
    ("\u2018", "'"), ("\u2019", "'"), ("\u201a", "'"), ("\u201b", "'"),
    ("\u201c", '"'), ("\u201d", '"'), ("\u201e", '"'), ("\u2032", "'"),
    ("\u00a0", " "), ("\u2007", " "), ("\u202f", " "), ("\u2009", " "),
    ("\u200b", ""), ("\u200c", ""), ("\u200d", ""), ("\ufeff", ""),
    ("\u2010", "-"), ("\u2011", "-"), ("\u2012", "-"), ("\u2013", "-"),
    ("\u2014", "-"), ("\u2015", "-"), ("\u2212", "-"),
)
_WS = re.compile(r"\s+")


@lru_cache(maxsize=2048)
def _norm_pattern(pattern: str) -> str:
    """A pattern, normalised. Cached: the list is walked once per ad."""
    return _norm_text(pattern)


def _norm_text(text: str) -> str:
    """
    Fold a string down so that matching is about WORDS, not typography.

    Matching was `pattern.lower() in text`, which quietly fails on four things
    that are everywhere in real ad copy:

      * curly quotes — "we’re hiring" (U+2019) against "we're hiring". Meta's own
        ads contain U+2019; this was found in text we crawled. So one of the
        category's own phrases had never matched anything.
      * a phrase split by a line break — "Apply\\nhere" is not "apply here"
      * non-breaking and zero-width spaces, invisible in the copy
      * dash variants — an en-dash is not a hyphen

    The text and the pattern are put through the SAME function, so neither side
    can be the odd one out. Normalising only one is how you end up with a pattern
    that still cannot match its own spacing.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    for src, dst in _CHAR_FOLDING:
        text = text.replace(src, dst)
    return _WS.sub(" ", text).strip().lower()


def passes_heuristics(
    ad: AdRecord,
    filters: ScrapingFilters,
    category=None,
) -> bool:
    """
    The heuristic backbone: explicit require/exclude ad-text patterns,
    then the CATEGORY's own tick/cross patterns, then the ad-age
    ceiling. The AI checkpoint never sees ads that fail here.

    Both the ad text and every pattern are normalised first, so what is compared
    is wording rather than punctuation. See _norm_text.
    """
    text = _norm_text(ad.ad_text)
    if not text:  # no body text at all → nothing to judge, reject
        return False

    for pattern in filters.require_ad_text_patterns:      # AND semantics
        if _norm_pattern(pattern) not in text:
            return False

    if filters.require_any_ad_text_patterns:              # OR semantics
        if not any(_norm_pattern(p) in text for p in filters.require_any_ad_text_patterns):
            return False

    for pattern in filters.exclude_ad_text_patterns:
        if _norm_pattern(pattern) in text:
            return False

    if category is not None:                              # category's own sieve
        heur = category.heuristics
        for pattern in heur.require_ad_text_patterns:
            if _norm_pattern(pattern) not in text:
                return False
        # The category's OR-vocabulary is honoured here too, not only via the
        # filters object. _build_filters already merges it in, so skipping it
        # worked by accident — but anything that reaches this function with a
        # raw ScrapingFilters would silently skip the category's own gate,
        # which is the kind of gap that looks like a filtering bug later.
        if heur.require_any_ad_text_patterns and not any(
            _norm_pattern(p) in text for p in heur.require_any_ad_text_patterns
        ):
            return False
        for pattern in heur.exclude_ad_text_patterns:
            if _norm_pattern(pattern) in text:
                return False

    if filters.ad_age_hours is not None and ad.ad_age_hours is not None:
        if ad.ad_age_hours > filters.ad_age_hours:
            return False

    return True


async def run_scrape(
    filters: ScrapingFilters,
    headless: bool = False,
    context: BrowserContext | None = None,
    run_seen_ids: RunSeenIds | None = None,
    profile_id: str | None = None,
    request_id: str | None = None,
    parallel_workers: int = 1,
    on_harvested=None,
    should_stop=None,
    on_step=None,
    profile_threshold: float = 0.5,
    request_threshold: float = 0.5,
) -> list[AdRecord]:
    """
    Full scrape pass: for each keyword, search the library, extract ads,
    apply heuristics, and return candidates that have no FINAL verdict
    yet. Marking happens only after the AI checkpoint — see modules.verdicts.
    No AI involved in this city.

    Browser ownership: pass `context` to reuse an already-open browser
    (the orchestrator holds ONE session for the entire run — relaunching
    between cycles wastes minutes and looks robotic). Without it, this
    call owns a fresh session and closes it before returning (smoke
    runner behavior).

    run_seen_ids: optional run-scoped shared set. The orchestrator passes one so
    the ids this pass harvests are visible to sibling workers; omit it (smoke
    runner, tests) and dedup stays in-process.
    profile_id: whose profile this run is under. Decides which rejections
    apply — accepted and junk are global, rejections are only this profile's.
    parallel_workers: how many keywords to crawl at once. 1 is the original
    single-page sequential behaviour; above 1 each worker gets its own page and
    a slice of the keywords.
    should_stop / on_step: polled per scroll step, see _scrape_keyword.
    """
    # The lens for this run — scopes verdict keys, heuristics and storage.
    cat_id = filters.category
    category = CATEGORIES.get(cat_id)
    if category is None:
        logger.warning(
            "⚠️ Unknown category %r — running without category heuristics",
            cat_id,
        )
    else:
        logger.info("🔎 Category lens: %s — %s", category.label, cat_id)

    run_seen = run_seen_ids or RunSeenIds()
    # In-memory mirror of this profile's verdicts (plus its ancestors'), loaded
    # once instead of one Redis round trip per scraped card. With no profile the
    # mirror is empty, which degrades to within-pass dedup only.
    filter_index = get_filter_index(
        cat_id, profile_id, request_id, profile_threshold, request_threshold,
    ).load()
    if profile_id is None:
        logger.warning("⚠️ No profile for this run — only global dedup is active")

    if context is not None:
        return await _scrape_on(context, filters, category, cat_id, run_seen,
                                 filter_index, profile_id, request_id,
                                 parallel_workers, on_harvested, should_stop, on_step)
    async with browser_session(headless=headless) as owned:
        return await _scrape_on(owned, filters, category, cat_id, run_seen,
                                filter_index, profile_id, request_id,
                                parallel_workers, on_harvested, should_stop, on_step)


async def _scrape_keyword(
    page: BrowserContext | Page,
    keyword: str,
    filters: ScrapingFilters,
    category,
    cat_id: str,
    run_seen: RunSeenIds,
    filter_index,
    profile_id: str | None,
    request_id: str | None,
    seen_ids: set[str],
    seen_prints: set[str],
    extract_lock: asyncio.Lock,
    is_first_keyword: bool,
    worker_label: str = "",
    should_stop=None,
    on_step=None,
    on_harvested=None,
) -> list[AdRecord]:
    """
    Crawl ONE keyword on ONE page and return the ads worth judging.

    `should_stop` is polled every scroll step. It causes a BREAK, never a raise:
    this function has one exit from the scroll loop, so breaking falls straight
    through to the filter below and the caller judges and stores whatever was
    collected. That is the whole reason cancellation loses no work — a keyword
    cut short behaves exactly like one that reached the bottom. Raising instead
    would be caught and swallowed by the parallel worker below, which would then
    carry on to the next keyword.
    `on_step` reports cards-seen-so-far per scroll, for liveness.

    `on_harvested` makes this keyword STREAM rather than accumulate: groups of ads
    are handed over mid-crawl, as soon as there are enough of them, instead of
    the whole keyword being returned at the end. Passed `None` — as the tests and
    the non-UI paths do — it keeps the old behaviour exactly and returns every
    candidate, so there is still one code path to reason about per mode rather
    than two divergent ones.

    Split out of the loop so the same body serves both the sequential path and
    each parallel worker. The only shared mutable state is `seen_ids`, and the
    one place it is read-then-written (`extract_ads_from_page`) is held under
    `extract_lock` so two workers cannot both claim the same card.
    """
    tag = f"[{worker_label}] " if worker_label else ""
    logger.info("%s▶️ Keyword: %r", tag, keyword)

    # One navigation per keyword — the crafted search URL is the whole trip.
    # Cookie dismissal rides on the first search page only.
    await search_keyword(page, keyword, filters, dismiss_cookies=is_first_keyword)
    await apply_filters(page, filters)

    # Estimate-aware scroll+extract: read Meta's own '~N results' header, then
    # crawl until we have seen TARGET_UNIQUE_RATIO of it, ending on quiet-step
    # budgets that differ by phase. Extraction yield, not scrollHeight, signals
    # progress.
    #
    # `keyword_cards` is this keyword's progress counter and is deliberately
    # NOT `seen_ids`: it counts every distinct ad id that appeared on screen
    # while scrolling THIS keyword, known or not, qualified or not. `seen_ids`
    # is the run-wide harvest, which is what made every keyword look finished
    # the moment the run had scraped enough of anything.
    keyword_cards: set[str] = set()
    keyword_ads: list[AdRecord] = []
    # Ids a SIBLING worker had already claimed before we looked. Resolved before
    # this keyword publishes its own ids, otherwise our own harvest looks like
    # someone else's and we would discard everything we just crawled.
    sibling_claimed: set[str] = set()

    # ── the two streaming buffers ───────────────────────────────────────────
    # `fetch_buffer` holds ads read off the page that have not yet been through
    # the cheap filters. `judge_buffer` holds the survivors, waiting for enough
    # of them to be worth the AI. See FETCH_BATCH / JUDGE_BATCH for why they are
    # separate numbers.
    fetch_buffer: list[AdRecord] = []
    judge_buffer: list[AdRecord] = []
    # With no callback there is nothing to stream to, so groups accumulate here
    # and are returned exactly as they were before streaming existed.
    fresh: list[AdRecord] = []
    already_final = deferred_renewed = heuristic_passed = 0
    judged_sent = 0
    judge_groups = 0

    def _already_decided(ad: AdRecord) -> bool:
        """Do we already have a final answer for this one? Asked on arrival.

        This is a set lookup, not a judgement: accepted lead, universal junk,
        this profile's rejections, what this request parked. Only the last can
        come back — a different request gets to judge a deferred ad again.
        Answering it here rather than at the buffer threshold is free, and it
        means ads we already know the answer for are never carried, never
        counted, and never occupy a slot in a batch we pay to judge.
        """
        nonlocal already_final, deferred_renewed
        if run_seen.shared and ad.ad_archive_id in sibling_claimed:
            # A sibling worker on this run took it first.
            already_final += 1
            return True
        if filter_index is None:
            return False
        skip, why = filter_index.check(ad.ad_archive_id)
        if not skip:
            return False
        already_final += 1
        if why == "deferred":
            # Passing over a deferred ad renews it — the whole cost of not
            # re-judging it under an unchanged request.
            if renew_deferral(profile_id, ad.ad_archive_id):
                deferred_renewed += 1
        return True

    async def _flush_fetch() -> int:
        """Run the heuristics over the fetch buffer. Survivors join the judge buffer."""
        nonlocal heuristic_passed
        moved = 0
        for ad in fetch_buffer:
            if not passes_heuristics(ad, filters, category):
                continue
            heuristic_passed += 1
            ad.category = cat_id  # stamp the lens — AI + storage read it
            judge_buffer.append(ad)
            moved += 1
        fetch_buffer.clear()
        return moved

    async def _flush_judge(force: bool = False) -> list[AdRecord]:
        """Hand the judge buffer over, if there is enough of it — or if forced.

        `force` is the end of a keyword, a Stop, or an idle run. All three mean
        the same thing: stop waiting and deal with what is on hand. A buffer
        holding three ads is still judged, because the alternative is throwing
        them away, and a group of one costs a round trip, not a fortune.

        Returns the group when there is no callback to send it to (the caller
        wants the ads back), and an empty list when it has already gone out by
        callback — so nothing is ever handed over twice.
        """
        nonlocal judged_sent, judge_groups
        if not judge_buffer:
            return []
        if not force and len(judge_buffer) < JUDGE_BATCH:
            return []
        group = list(judge_buffer)
        judge_buffer.clear()
        judged_sent += len(group)
        judge_groups += 1
        if on_harvested is None:
            return group          # no callback: hand them back to the caller
        await _report_harvest(on_harvested, group, keyword, len(seen_ids))
        return []

    async def _drain() -> list[AdRecord]:
        """Everything still buffered, judged. Used on every exit path."""
        await _flush_fetch()
        return await _flush_judge(force=True)

    # Meta's own zero-results signal: skip the entire crawl. Scrolling an empty
    # page is pointless and looks robotic, and it is not a failure — so it gets
    # its own plain message rather than falling through to the crawl loop and
    # reporting a budget that was never spent.
    if await empty_results(page):
        logger.info("%s✅ %r: Meta reports no results for this keyword", tag, keyword)
        return []

    estimate = await read_results_estimate(page)
    target_unique = int(estimate * TARGET_UNIQUE_RATIO) if estimate else None
    quiet_budget = QUIET_STEPS_BEFORE_TARGET  # shortened once we get there
    quiet_steps = 0
    steps = 0
    reached_target = False
    # Cards already on screen before this step, so a step can be judged on what
    # the page added rather than on what was new to the run.
    cards_before = 0

    # Per-step timings, so where this keyword's wall-clock went is visible
    # rather than guessed at. Tuning a crawl without these means guessing.
    dom_cards: int | None = None
    waited: list[int] = []
    # Time spent waiting on Meta rather than scrolling. Kept apart from `waited`
    # so the summary can say which of the two dominated the keyword's wall clock:
    # these numbers are what explain a crawl that took 14s instead of 2s.
    waited_total_ms = 0
    loads_waited = 0
    # Consecutive steps that ended at the bottom with Meta promising nothing and
    # delivering nothing. This is how "the list ran out" is told apart from "the
    # crawl gave up", which the quiet budget on its own cannot do.
    bottoms_confirmed = 0
    stuck_scrolls = 0
    stuck_reloads = 0

    logger.info(
        "%s🎯 Crawl plan: Meta says ~%s results, target %s cards seen, exit after %d dead scroll(s), "
        "fixed %dpx steps, waiting for Meta's lazy loads rather than racing them",
        tag, estimate if estimate is not None else "?", target_unique, quiet_budget,
        SCROLL_DISTANCE_PX,
    )

    while True:
        steps += 1

        if should_stop is not None and should_stop():
            # Break, not raise — see the docstring. Everything harvested from
            # this keyword so far is below in `keyword_ads` and is judged and
            # saved like any other keyword; only the cards we never scrolled to
            # are gone, which was true whether or not anyone pressed stop.
            logger.warning(
                "%s🛑 Cancelled mid-keyword %r — %d card(s) collected so far. "
                "They will be judged and saved; the rest of this list was not searched.",
                tag, keyword, len(keyword_ads),
            )
            break

        # Serialised: extraction filters against the shared seen_ids, and it
        # must be read-then-written atomically or two workers harvest one card.
        async with extract_lock:
            new_ads = await extract_ads_from_page(
                page, source_keyword=keyword, seen_ids=seen_ids,
                seen_fingerprints=seen_prints, encountered=keyword_cards,
            )
        step_ids = [ad.ad_archive_id for ad in new_ads]
        # Remember what a sibling had claimed, then publish ours.
        sibling_claimed.update(ad_id for ad_id in step_ids if run_seen.has(ad_id))
        run_seen.add_many(step_ids)

        # Progress is what the page SHOWED us, not what was NEW to us. A scroll
        # that returns six already-known cards is content, not a dead end, so
        # the quiet budget must not be spent on it — that is how a keyword used
        # to be declared exhausted while Meta was still handing out results.
        cards_seen = len(keyword_cards)
        if cards_seen > cards_before:
            quiet_steps = 0
        elif not stuck_scrolls:
            # Only a step that actually scrolled can be evidence of the bottom.
            quiet_steps += 1
        cards_before = cards_seen

        if new_ads:
            keyword_ads.extend(new_ads)
            # Straight into the fetch buffer, minus anything we already have a
            # final answer for. That question is free to ask, so it is asked
            # here rather than at the threshold — see _already_decided.
            fetch_buffer.extend(ad for ad in new_ads if not _already_decided(ad))
            if len(fetch_buffer) >= FETCH_BATCH:
                await _flush_fetch()
                # Empty when a callback took the group; the ads themselves when
                # there is none, so callers that want them back get them.
                fresh.extend(await _flush_judge())

        # Hitting the target does not end the crawl. It only buys the shorter
        # dead-end budget, so the page gets to confirm the bottom.
        if not reached_target and target_unique is not None and cards_seen >= target_unique:
            reached_target = True
            logger.info("%s🏁 %r: %d/%d cards seen — target reached, confirming the tail",
                        tag, keyword, cards_seen, estimate)
        quiet_budget = QUIET_STEPS_AFTER_TARGET if reached_target else QUIET_STEPS_BEFORE_TARGET

        if quiet_steps >= quiet_budget:
            if target_unique is None:
                logger.info("%s🛑 %r: %d dead scroll(s) with no result count to measure against — stopping",
                            tag, keyword, quiet_steps)
            elif reached_target:
                logger.info("%s✅ %r: %d/%d cards seen, then %d dead scroll(s) — bottom reached",
                            tag, keyword, cards_seen, estimate, quiet_steps)
            elif bottoms_confirmed >= 2:
                # We stopped short of the target, but not because the crawl
                # failed: the page sat at the bottom, promised nothing further,
                # and handed over nothing further, twice over. Meta's own header
                # is a rounded guess and it over-states as often as it
                # under-states — 'python developer' reads ~1,400 and yields 954.
                # Calling that a failure trains us to ignore the warning, which
                # is how a real one gets missed.
                logger.info(
                    "%s✅ %r: bottom reached at %d card(s) — Meta's header said ~%s, "
                    "so the list ran out before the target. Not a failure.",
                    tag, keyword, cards_seen, estimate,
                )
            else:
                logger.warning(
                    "%s⚠️  %r: ABORTED after %d dead scroll(s) at %d/%d cards (target was %d). "
                    "The page stopped producing before this keyword was exhausted — treat this "
                    "as a crawl failure, not as 'this keyword had nothing'.",
                    tag, keyword, quiet_steps, cards_seen, estimate, target_unique,
                )
            break

        # Scroll a fixed distance, then wait for the cards that scroll asked for
        # rather than sleeping a fixed 1.5s and hoping. The distance never
        # changes: growing it is what lets a jump outrun the virtualised
        # renderer and skip cards, and a skipped card is one we never know we
        # lost. More round trips, no silent gaps.
        if dom_cards is None:
            from .search import _dom_card_count

            try:
                dom_cards = await _dom_card_count(page)
            except Exception:  # noqa: BLE001
                dom_cards = 0
        timing = await scroll_step(page, distance=SCROLL_DISTANCE_PX, baseline=dom_cards)
        if timing.get("count") is not None:
            dom_cards = timing["count"]
        waited.append(timing["waited_ms"])

        # ── patience ──────────────────────────────────────────────────────────
        # A results page that will not move is usually not stuck, it is busy.
        # Meta lazy-loads the next batch when you reach the bottom, shows a
        # spinner or a 'see more' while it fetches, and does not move until the
        # batch lands. The old loop asked milliseconds later, could not tell
        # busy from finished, and called it the end of the keyword — measured
        # live on a keyword Meta estimates ~1,400 results for: 15 scrolls, 25
        # ads, and 12 of those scrolls reporting 'stopped moving' while Meta was
        # still handing results out.
        #
        # So: ask whether more is promised, and if it is, wait for it. The
        # budget is not replaced by this and not weakened by it — a promise that
        # never turns into ads is reported as a stall, and the budget still gets
        # the last word. All this adds is the difference between "not moving
        # yet" and "not moving any more".
        moved = bool(timing.get("moved", True))
        probe = await more_coming(page)
        promised = bool(probe.get("spinner") or probe.get("see_more"))
        load = None

        if promised or not moved:

            async def _beat():
                """Report in while we wait, so a keyword legitimately waiting on
                Meta is never mistaken for a wedged run."""
                if on_step is None:
                    return
                try:
                    await on_step(
                        keyword=keyword,
                        cards_seen=len(keyword_cards),
                        ads_collected=len(keyword_ads),
                        target=target_unique,
                        estimate=estimate,
                        steps=steps,
                    )
                except Exception as exc:  # noqa: BLE001 - heartbeat must not break a crawl
                    logger.debug("⚠️ wait heartbeat failed (%s)", exc)

            load = await wait_for_more_ads(page, on_poll=_beat)
            waited.append(load["waited_ms"])
            waited_total_ms += load["waited_ms"]
            loads_waited += 1

        grew = bool(load and load["grew"])
        # "At the bottom" is a claim about the PAGE — we are as low as it goes
        # and it is offering nothing more. It is NOT implied by the wait simply
        # returning empty-handed: a page we could not even read, or one that is
        # frozen with nothing to show for it, returns empty-handed too. Reading
        # that as "bottom" is what would let a dead tab sit there quietly, and it
        # is why only the explicit reasons count here.
        bottom = bool(load and load["reason"] == "bottom") or (
            not promised and bool(probe.get("atBottom"))
        )
        stalled = bool(load and load["reason"] in ("promised_but_stalled", "timeout"))

        # Liveness. Judging happens once per KEYWORD, so a keyword promising
        # ~880 results is silent for its whole ten minutes — without this tick
        # there is no way to tell a working crawl from a wedged tab, and no way
        # to tell whether a run has stalled.
        if on_step is not None:
            try:
                await on_step(
                    keyword=keyword,
                    cards_seen=len(keyword_cards),
                    ads_collected=len(keyword_ads),
                    target=target_unique,
                    estimate=estimate,
                    steps=steps,
                )
            except Exception as exc:  # noqa: BLE001 - reporting must never break a crawl
                logger.debug("⚠️ step reporting failed (%s)", exc)

        if grew:
            # Meta kept its promise. The next extraction picks the new cards up,
            # so the quiet budget starts again and any earlier stuck tally is
            # forgiven — a page that loads is not a page that is stuck.
            quiet_steps = 0
            stuck_scrolls = 0
            bottoms_confirmed = 0
            logger.info(
                "%s⏳ %r: page said more was coming and delivered — %d card(s) now on screen "
                "(waited %dms)", tag, keyword, load["ads"], load["waited_ms"],
            )
        elif stalled or (not moved and not bottom):
            # Only now is a still page a fault: Meta promised results that never
            # arrived, or the page stopped responding with nothing to show for
            # it. Before this, ANY scroll that did not move landed here — which
            # meant the bottom of a healthy list reloaded the tab and then gave
            # up on the keyword.
            stuck_scrolls += 1
            bottoms_confirmed = 0
            logger.warning(
                "%s🪦 %r: the page stopped responding (%d step(s) so far)%s",
                tag, keyword, stuck_scrolls,
                " — Meta promised more and never sent it"
                if stalled else " — no promise of more, and nothing arrived",
            )
            if stuck_scrolls >= STUCK_SCROLL_LIMIT:
                stuck_reloads += 1
                if stuck_reloads > MAX_STUCK_RELOADS:
                    # Bounded, because every reload also resets the quiet
                    # budget: without this, a tab that never recovers would
                    # reload forever and never reach a dead-end exit. This is a
                    # fault, not an exhaustion, and it is logged as one.
                    logger.error(
                        "%s💥 %r: tab is unusable — %d stuck scroll(s) across %d reload(s). "
                        "Abandoning this keyword; the page, not the keyword, is the problem.",
                        tag, keyword, stuck_scrolls, stuck_reloads - 1,
                    )
                    break
                # Recovering means putting the tab back in a known state. A
                # reload is the only thing that clears a dialog Playwright
                # cannot click. It also drops us at the TOP of the results, so
                # the depth is saved and restored: otherwise every stuck pane
                # costs us the whole list again, and since already-seen cards
                # bank nothing, re-crawling the top reads as a dead end and ends
                # the keyword we were trying to rescue.
                depth = await scroll_depth(page)
                logger.warning(
                    "%s♻️  %r: %d stuck scroll(s) — reloading the tab, resuming from %dpx",
                    tag, keyword, stuck_scrolls, depth,
                )
                try:
                    await page.reload(wait_until="domcontentloaded")
                    await search_keyword(page, keyword, filters, dismiss_cookies=True)
                    await apply_filters(page, filters)
                    landed = await restore_scroll_depth(page, depth)
                    # A reload is a genuinely fresh page, so the dead-end tally
                    # starts again rather than being inherited from the tab that
                    # just broke.
                    quiet_steps = 0
                    cards_before = len(keyword_cards)
                    stuck_scrolls = 0
                    dom_cards = None
                    if landed < depth:
                        logger.warning(
                            "%s⚠️  %r: could only resume to %dpx of %dpx — re-crawling from the top",
                            tag, keyword, landed, depth,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.error("%s💥 %r: reload failed (%s) — ending this keyword",
                                 tag, keyword, exc)
                    break
        elif bottom:
            # We are as low as the page goes, it is offering nothing more, and
            # it handed over nothing this step either. That is the bottom, not a
            # fault — counted here so the summary can say so honestly when the
            # list runs out below Meta's estimate.
            bottoms_confirmed += 1

    if not keyword_ads:
        # Nothing matched the selectors — dump the results page so the card
        # selectors can be tuned against the real DOM. (A Meta empty state is a
        # KNOWN condition and returned above, so it never reaches here.)
        await collect_page_html(page, tag=f"no_cards_{keyword.replace(' ', '_')}")

    # Every way out of the loop above lands here — the bottom, a Stop, an
    # unusable tab — and all of them mean the same thing: stop waiting and deal
    # with what is on hand. One drain, so no exit can forget it and quietly
    # discard a partial group. In streaming mode `fresh` stays empty and the groups
    # went out mid-crawl; without a callback this is the whole of the old
    # per-keyword behaviour.
    fresh.extend(await _drain())

    # Tell the run how this keyword's funnel actually came out.
    #
    # This was logged and nothing more, so the one number that answers "did Meta
    # have nothing, or did the sieve eat it?" was only visible by digging through
    # a log file after the fact. The sieve is doing its job — fetched ads are
    # always more than judged ads — but a keyword that yields almost nothing
    # looks identical whether the list was thin or the filter was harsh.
    if on_step is not None:
        try:
            await on_step(
                keyword=keyword,
                cards_seen=len(keyword_cards),
                ads_collected=len(keyword_ads),
                steps=steps,
                sieve_done=True,
                sieve={
                    "cards": len(keyword_cards),
                    "already_decided": already_final,
                    "kept": heuristic_passed,
                    "to_ai": judged_sent,
                },
            )
        except Exception as exc:  # noqa: BLE001 - reporting must never break a crawl
            logger.debug("⚠️ sieve reporting failed (%s)", exc)

    logger.info(
        "%s🧺 Keyword %r: %d ad card(s) seen on this list (%d new to the run; %d run-wide unique) — "
        "%d already decided%s, %d passed heuristics -> %d to AI%s",
        tag, keyword, len(keyword_cards), len(keyword_ads), len(seen_ids), already_final,
        f" ({deferred_renewed} deferrals renewed)" if deferred_renewed else "",
        heuristic_passed, judged_sent,
        f" in {judge_groups} group(s)" if judge_groups > 1 else "",
    )
    if waited:
        logger.info(
            "%s⏱️  %r: %d scroll(s) of %dpx, %dms waiting for content (avg %dms)",
            tag, keyword, len(waited), SCROLL_DISTANCE_PX, sum(waited),
            sum(waited) // len(waited),
        )
    if loads_waited:
        # Said plainly, because a crawl that suddenly takes 15s a keyword looks
        # like a regression and is actually Meta being waited for correctly.
        logger.info(
            "%s⏳  %r: waited on Meta %d time(s), %dms of it",
            tag, keyword, loads_waited, waited_total_ms,
        )
    return fresh


def _chunk(items: list[str], buckets: int) -> list[list[str]]:
    """Round-robin the keywords so every worker gets a similar-sized slice."""
    buckets = max(1, min(buckets, len(items)))
    out: list[list[str]] = [[] for _ in range(buckets)]
    for i, item in enumerate(items):
        out[i % buckets].append(item)
    return [chunk for chunk in out if chunk]


async def _report_harvest(on_harvested, batch: list, keyword: str, total_unique: int) -> None:
    """
    Hand a finished keyword's ads to the caller.

    Never allowed to break the run: a reporting hook that raises would lose the
    crawl it was reporting on, which is the opposite of what it is for.
    """
    try:
        result = on_harvested(batch, keyword, total_unique)
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Harvest reporting failed for %r (%s) — continuing", keyword, exc)


async def _scrape_on(
    context: BrowserContext,
    filters: ScrapingFilters,
    category,
    cat_id: str,
    run_seen: RunSeenIds,
    filter_index=None,
    profile_id: str | None = None,
    request_id: str | None = None,
    parallel_workers: int = 1,
    on_harvested=None,
    should_stop=None,
    on_step=None,
) -> list[AdRecord]:
    """
    Crawl every keyword on an existing context.

    `parallel_workers` = 1 runs the original single-page sequential loop. Above
    1, each worker gets its OWN page and a round-robin slice of the keywords,
    with the run-scoped seen set shared so no ad is harvested twice.

    Configurable rather than hardcoded: concurrency multiplies captcha exposure,
    so being able to drop back to 1 without a code change matters.
    """
    keywords = list(filters.keywords)
    if not keywords:
        return []

    # Shared across EVERY keyword in this pass and every worker on it. One ad
    # usually matches several keywords, and re-harvesting it per keyword is what
    # produced duplicate rows in the run files.
    seen_ids: set[str] = set()
    # Same ads, different Library IDs. Meta issues one id per platform, so an
    # id set alone cannot tell one creative from five copies of it.
    seen_prints: set[str] = set()
    extract_lock = asyncio.Lock()

    workers = max(1, min(int(parallel_workers or 1), len(keywords)))
    if workers == 1:
        logger.info("🔁 Scraping %d keyword(s) sequentially", len(keywords))
        page = await context.new_page()
        try:
            fresh: list[AdRecord] = []
            for idx, keyword in enumerate(keywords):
                if should_stop is not None and should_stop():
                    logger.info("🔫 Stopping before %r — %d keyword(s) left unsearched",
                                keyword, len(keywords) - idx)
                    break
                batch = await _scrape_keyword(
                    page, keyword, filters, category, cat_id, run_seen, filter_index,
                    profile_id, request_id, seen_ids, seen_prints,
                    extract_lock, is_first_keyword=(idx == 0),
                    should_stop=should_stop, on_step=on_step,
                    on_harvested=on_harvested,
                )
                fresh.extend(batch)
                # The keyword now reports its own groups mid-crawl, so it returns
                # nothing to report here. This branch only fires for callers that
                # passed no callback — the tests and the non-UI paths — and it is
                # the whole of the old per-keyword behaviour for them.
        finally:
            await page.close()
    else:
        # Cross-worker dedup now genuinely needs the shared set, not just the
        # in-process one.
        run_seen.shared = True
        buckets = _chunk(keywords, workers)
        logger.info(
            "⚡ Scraping %d keyword(s) across %d parallel worker(s): %s",
            len(keywords), len(buckets),
            ", ".join(f"{i+1}:{len(c)}" for i, c in enumerate(buckets)),
        )
        pages = [await context.new_page() for _ in buckets]

        async def worker(idx: int, chunk: list[str]) -> list[AdRecord]:
            out: list[AdRecord] = []
            label = f"w{idx + 1}"
            for j, keyword in enumerate(chunk):
                # Checked here too: a worker that only noticed cancellation
                # inside a keyword would still start the NEXT one.
                if should_stop is not None and should_stop():
                    logger.info("🔫 %s stopping before %r", label, keyword)
                    break
                try:
                    batch = await _scrape_keyword(
                        pages[idx], keyword, filters, category, cat_id, run_seen,
                        filter_index, profile_id, request_id, seen_ids,
                        seen_prints, extract_lock,
                        is_first_keyword=(idx == 0 and j == 0),
                        worker_label=label,
                        should_stop=should_stop, on_step=on_step,
                        on_harvested=on_harvested,
                    )
                    out.extend(batch)
                except Exception as exc:  # noqa: BLE001 - one bad keyword must not kill the run
                    logger.error("💥 %s keyword %r failed: %s", label, keyword, exc, exc_info=True)
            return out

        try:
            results = await asyncio.gather(
                *(worker(i, chunk) for i, chunk in enumerate(buckets)),
                return_exceptions=True,
            )
        finally:
            for page in pages:
                try:
                    await page.close()
                except Exception:  # noqa: BLE001 - best effort on teardown
                    pass

        fresh = []
        for i, result in enumerate(results):
            if isinstance(result, BaseException):
                logger.error("💥 worker %d died: %s", i + 1, result)
            else:
                fresh.extend(result)

    # Belt and braces: workers append concurrently, so dedupe once more here.
    # Cheap, and it makes "no duplicate ad ids in a run" a property of the
    # function rather than a hope about the workers.
    if workers > 1:
        unique: list[AdRecord] = []
        taken: set[str] = set()
        for ad in fresh:
            if ad.ad_archive_id in taken:
                continue
            taken.add(ad.ad_archive_id)
            unique.append(ad)
        if len(unique) != len(fresh):
            logger.warning(
                "🧹 Collapsed %d cross-worker duplicate(s) before judging",
                len(fresh) - len(unique),
            )
        fresh = unique

    logger.info("✅ Scrape complete: %d fresh ads across %d keywords (%d worker(s))",
                len(fresh), len(keywords), workers)
    return fresh
