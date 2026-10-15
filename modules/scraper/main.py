"""
main.py — the citizens of the scraper city.

One job: turn a RunConfig's filters into fresh, deduplicated AdRecords.
Everything heavier (browser opening, filter clicks, card extraction) is
packaged in this city's warehouses (browser.py / search.py / extract.py).
"""

import asyncio
import inspect
import logging

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
    read_results_estimate,
    restore_scroll_depth,
    scroll_depth,
    scroll_step,
    search_keyword,
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
#   TARGET_UNIQUE_RATIO — the safety margin. 0.80 means "aim for 80% of what
#     Meta estimated, then let the dead-end rule confirm the rest": the count
#     rounds up, so a list that reaches 80% and then goes quiet is a healthy
#     finish, not a shortfall.
#
# There is deliberately NO step cap and no scroll-distance ceiling. A crawl
# ends when the page stops giving us cards, never because a counter ran out —
# and repeats bank nothing, so a page that has gone dry always reaches its
# dead-end budget on its own. What IS bounded is the stuck-pane recovery below:
# reloading a tab that is already broken just produces another broken tab.
TARGET_UNIQUE_RATIO = 0.80
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


def passes_heuristics(
    ad: AdRecord,
    filters: ScrapingFilters,
    category=None,
) -> bool:
    """
    The heuristic backbone: explicit require/exclude ad-text patterns,
    then the CATEGORY's own tick/cross patterns, then the ad-age
    ceiling. The AI checkpoint never sees ads that fail here.
    """
    text = ad.ad_text.lower()
    if not text.strip():  # no body text at all → nothing to judge, reject
        return False

    for pattern in filters.require_ad_text_patterns:      # AND semantics
        if pattern.lower() not in text:
            return False

    if filters.require_any_ad_text_patterns:              # OR semantics
        if not any(p.lower() in text for p in filters.require_any_ad_text_patterns):
            return False

    for pattern in filters.exclude_ad_text_patterns:
        if pattern.lower() in text:
            return False

    if category is not None:                              # category's own sieve
        heur = category.heuristics
        for pattern in heur.require_ad_text_patterns:
            if pattern.lower() not in text:
                return False
        # The category's OR-vocabulary is honoured here too, not only via the
        # filters object. _build_filters already merges it in, so skipping it
        # worked by accident — but anything that reaches this function with a
        # raw ScrapingFilters would silently skip the category's own gate,
        # which is the kind of gap that looks like a filtering bug later.
        if heur.require_any_ad_text_patterns and not any(
            p.lower() in text for p in heur.require_any_ad_text_patterns
        ):
            return False
        for pattern in heur.exclude_ad_text_patterns:
            if pattern.lower() in text:
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
) -> list[AdRecord]:
    """
    Full scrape pass: for each keyword, search the library, extract ads,
    apply heuristics, and return candidates that have no FINAL verdict
    yet. Marking happens only after the AI checkpoint — see SeenIndex.
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
    filter_index = get_filter_index(cat_id, profile_id, request_id).load()
    if profile_id is None:
        logger.warning("⚠️ No profile for this run — only global dedup is active")

    if context is not None:
        return await _scrape_on(context, filters, category, cat_id, run_seen,
                                 filter_index, profile_id, request_id,
                                 parallel_workers, on_harvested)
    async with browser_session(headless=headless) as owned:
        return await _scrape_on(owned, filters, category, cat_id, run_seen,
                                filter_index, profile_id, request_id,
                                parallel_workers, on_harvested)


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
) -> list[AdRecord]:
    """
    Crawl ONE keyword on ONE page and return the ads worth judging.

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
    stuck_scrolls = 0
    stuck_reloads = 0

    logger.info(
        "%s🎯 Crawl plan: Meta says ~%s results, target %s cards seen, exit after %d dead scroll(s), "
        "fixed %dpx steps",
        tag, estimate if estimate is not None else "?", target_unique, quiet_budget,
        SCROLL_DISTANCE_PX,
    )

    while True:
        steps += 1
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

        if not timing.get("moved", True):
            stuck_scrolls += 1
            logger.warning(
                "%s🪦 %r: the results pane stopped responding (%d step(s) so far)",
                tag, keyword, stuck_scrolls,
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

    if not keyword_ads:
        # Nothing matched the selectors — dump the results page so the card
        # selectors can be tuned against the real DOM. (A Meta empty state is a
        # KNOWN condition and returned above, so it never reaches here.)
        await collect_page_html(page, tag=f"no_cards_{keyword.replace(' ', '_')}")

    fresh: list[AdRecord] = []
    already_final = deferred_renewed = heuristic_passed = 0
    for ad in keyword_ads:
        # One decision per card, answered from memory: already-accepted lead,
        # then universal junk, then this profile's own rejections, then what
        # this request parked. Only the last can come back — a different
        # request gets to judge a deferred ad again.
        if filter_index is not None:
            skip, why = filter_index.check(ad.ad_archive_id)
            if skip:
                already_final += 1
                if why == "deferred":
                    # Passing over a deferred ad renews it — the whole cost of
                    # not re-judging it under an unchanged request.
                    if renew_deferral(profile_id, ad.ad_archive_id):
                        deferred_renewed += 1
                continue
        if run_seen.shared and ad.ad_archive_id in sibling_claimed:
            # A sibling worker on this run took it first.
            already_final += 1
            continue
        if not passes_heuristics(ad, filters, category):
            continue
        heuristic_passed += 1
        ad.category = cat_id  # stamp the lens — AI + storage read it
        fresh.append(ad)

    logger.info(
        "%s🧺 Keyword %r: %d ad card(s) seen on this list (%d new to the run; %d run-wide unique) — "
        "%d already decided%s, %d passed heuristics -> %d to AI",
        tag, keyword, len(keyword_cards), len(keyword_ads), len(seen_ids), already_final,
        f" ({deferred_renewed} deferrals renewed)" if deferred_renewed else "",
        heuristic_passed, len(fresh),
    )
    if waited:
        logger.info(
            "%s⏱️  %r: %d scroll(s) of %dpx, %dms waiting for content (avg %dms)",
            tag, keyword, len(waited), SCROLL_DISTANCE_PX, sum(waited),
            sum(waited) // len(waited),
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
                batch = await _scrape_keyword(
                    page, keyword, filters, category, cat_id, run_seen, filter_index,
                    profile_id, request_id, seen_ids, seen_prints,
                    extract_lock, is_first_keyword=(idx == 0),
                )
                fresh.extend(batch)
                # Report each keyword the moment it lands. A run that scrapes
                # for an hour before returning anything gives the UI nothing to
                # show, and a crash anywhere in that hour loses every ad.
                if on_harvested and batch:
                    await _report_harvest(on_harvested, batch, keyword, len(seen_ids))
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
                try:
                    batch = await _scrape_keyword(
                        pages[idx], keyword, filters, category, cat_id, run_seen,
                        filter_index, profile_id, request_id, seen_ids,
                        seen_prints, extract_lock,
                        is_first_keyword=(idx == 0 and j == 0),
                        worker_label=label,
                    )
                    out.extend(batch)
                    if on_harvested and batch:
                        await _report_harvest(on_harvested, batch, keyword, len(seen_ids))
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
