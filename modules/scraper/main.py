"""
main.py — the citizens of the scraper city.

One job: turn a RunConfig's filters into fresh, deduplicated AdRecords.
Everything heavier (browser opening, filter clicks, card extraction) is
packaged in this city's warehouses (browser.py / search.py / extract.py).
"""

import asyncio
import logging

from playwright.async_api import BrowserContext, Page

from core import (
    AdRecord,
    CATEGORIES,
    ScrapingFilters,
    safe_redis_operation,
    verdict_broker,
    compute_profile_hashes,
    PROFILE_HASH_TTL,
)

from modules.families.index import get_family_index

from .browser import browser_session
from .extract import extract_ads_from_page
from .seen import RunSeenIds
from .search import (
    apply_filters,
    collect_page_html,
    empty_results,
    read_results_estimate,
    scroll_step,
    search_keyword,
)

logger = logging.getLogger(__name__)


def renew_deferral(family_id: str | None, ad_id: str) -> bool:
    """
    Extend a deferred ad's TTL when we pass over it.

    A deferred ad met again under the SAME request is not re-judged — the
    request has not changed, so nothing about it has changed. Renewing here is
    what keeps it alive in the not-now bucket instead of quietly ageing out.
    Imported lazily so the scraper city does not depend on the family package
    at import time.
    """
    if not family_id:
        return False
    from modules.families.deferred import renew

    return renew(family_id, ad_id)

# Verdict key pattern in Redis DB 1 — first writer wins, and the key is
# CATEGORY+PROFILE-SCOPED: a verdict is about an ad UNDER A LENS + PROFILE,
# so profile changes automatically get a fresh key space.
#   seen_ad = a FINAL verdict was reached (accepted OR judged-rejected):
#             never processed again IN THIS CATEGORY+PROFILE. AI-failure ads
#             stay unmarked on purpose, so they flow through and get
#             re-judged next run — the pass-through lives in WHAT gets
#             marked, not in an extra filter condition.
SEEN_AD_KEY = "tesserae:seen_ad:{category}:{about_hash}:{filter_hash}:{ad_id}"

# Scroll+extract loop tuning, driven by Meta's own '~N results' estimate:
#   - target_unique  = 80% of the estimate — the safety margin. The list
#     rounds up (viewer throttling, non-card results), so 80% is a
#     realistic best-case unique harvest; reaching it is 'done', not a
#     failure.
#   - quiet budgets: BELOW target, tolerate 10 consecutive no-new-cards
#     steps (latency/network blips must not end the crawl); PAST target,
#     3 consecutive quiet steps ends it (estimate lied / exhausted).
#   - hard cap = 3x the estimate-derived step guess (~8 cards/step, set
#     by estimate size), so even a truthful huge estimate can't spin
#     forever if extraction keeps under-delivering.
TARGET_UNIQUE_RATIO = 0.80
QUIET_STEPS_BEFORE_TARGET = 5
QUIET_STEPS_AFTER_TARGET = 3
CARDS_PER_STEP_GUESS = 8
HARD_STEP_CAP_MULTIPLIER = 3.0

# Consecutive scroll steps in which EVERY harvested card already has a verdict
# before we abandon a keyword. Kept above 1 on purpose: with an overlapping
# keyword set a burst of known cards is normal at the top of a result page,
# and bailing there would under-collect exactly when dedup is doing its job.
ALL_KNOWN_STEPS_BEFORE_EXIT = 4


def _scroll_budget(estimate: int | None) -> int:
    """Hard scroll-step cap derived from the estimate (fallback: 15)."""
    if estimate is None:
        return 15
    return max(10, min(120, int(HARD_STEP_CAP_MULTIPLIER * estimate / CARDS_PER_STEP_GUESS)))


def already_fetched(ad_id: str, category: str, filters: ScrapingFilters | None = None) -> bool:
    """
    True if this ad has a FINAL verdict under this category+profile from a
    previous run. Marks are written only AFTER the AI checkpoint reaches
    a verdict (accepted, or model-judged-rejected) — ads lost to AI
    failure stay unmarked and get another chance next run.

    Profile hashes are included in the key, so profile changes automatically
    get a fresh key space (old verdicts don't block re-evaluation).
    """
    about_hash, filter_hash = compute_profile_hashes()
    key = SEEN_AD_KEY.format(category=category, about_hash=about_hash, filter_hash=filter_hash, ad_id=ad_id)
    return safe_redis_operation(verdict_broker.exists, key) == 1


def mark_fetched(ad_id: str, category: str, filters: ScrapingFilters | None = None) -> None:
    """
    Record an ad id as VERDICT-REACHED under this category+profile — called
    only after checkpoint (a) has judged the ad (accepted, or
    model-judged-rejected), never for a mere heuristic survivor.
    AI-failure ads are deliberately left unmarked so a future run
    re-judges them.

    Key includes profile hashes and TTL, so profile changes get fresh space
    and old keys auto-expire after 90 days.
    """
    about_hash, filter_hash = compute_profile_hashes()
    key = SEEN_AD_KEY.format(category=category, about_hash=about_hash, filter_hash=filter_hash, ad_id=ad_id)
    safe_redis_operation(verdict_broker.set, key, 1, ex=PROFILE_HASH_TTL)


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
    family_id: str | None = None,
    request_family_id: str | None = None,
    parallel_workers: int = 1,
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
    family_id: the family whose verdicts decide what to skip. Without it the
    only dedup available is within this pass — callers should resolve the
    active family rather than skip this.
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
    # In-memory mirror of this family's verdicts (plus its ancestors'), loaded
    # once instead of one Redis round trip per scraped card. With no family the
    # mirror is empty, which degrades to within-pass dedup only.
    verdict_index = get_family_index(cat_id, family_id) if family_id else None
    if verdict_index is None:
        logger.warning("⚠️ No family for this run — only within-pass dedup is active")

    if context is not None:
        return await _scrape_on(context, filters, category, cat_id, run_seen,
                                 verdict_index, family_id, request_family_id,
                                 parallel_workers)
    async with browser_session(headless=headless) as owned:
        return await _scrape_on(owned, filters, category, cat_id, run_seen,
                                verdict_index, family_id, request_family_id,
                                parallel_workers)


async def _scrape_keyword(
    page: BrowserContext | Page,
    keyword: str,
    filters: ScrapingFilters,
    category,
    cat_id: str,
    run_seen: RunSeenIds,
    verdict_index,
    family_id: str | None,
    request_family_id: str | None,
    seen_ids: set[str],
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
    # crawl until 80% of it is uniquely harvested, ending on quiet-step budgets
    # that differ by phase. Extraction yield, not scrollHeight, signals progress.
    keyword_start_unique = len(seen_ids)
    keyword_ads: list[AdRecord] = []
    all_known_steps = 0
    # Ids a SIBLING worker had already claimed before we looked. Resolved before
    # this keyword publishes its own ids, otherwise our own harvest looks like
    # someone else's and we would discard everything we just crawled.
    sibling_claimed: set[str] = set()

    # Meta's own zero-results signal: skip the entire crawl — scrolling an
    # empty page is pointless and looks robotic.
    empty_page = await empty_results(page)
    if empty_page:
        logger.info("%s🚫 No ads match %r — skipping crawl", tag, keyword)
        estimate = None
        target_unique = None
        quiet_budget = QUIET_STEPS_AFTER_TARGET
        hard_steps = 0  # the crawl loop simply never runs
    else:
        estimate = await read_results_estimate(page)
        target_unique = int(estimate * TARGET_UNIQUE_RATIO) if estimate else None
        quiet_budget = QUIET_STEPS_AFTER_TARGET  # tightest; loosened below
        hard_steps = _scroll_budget(estimate)
    quiet_steps = 0
    steps = 0

    logger.info(
        "%s🎯 Crawl plan: estimate~%s, target %s unique, quiet budget %s, cap %d steps",
        tag, estimate if estimate is not None else "?", target_unique, quiet_budget, hard_steps,
    )

    while steps < hard_steps:
        steps += 1
        # Serialised: extraction filters against the shared seen_ids, and it
        # must be read-then-written atomically or two workers harvest one card.
        async with extract_lock:
            new_ads = await extract_ads_from_page(
                page, source_keyword=keyword, seen_ids=seen_ids
            )
        step_ids = [ad.ad_archive_id for ad in new_ads]
        # "Already decided" has to be answered BEFORE we publish this step, or
        # every ad we just harvested would count as known and trip the early
        # exit on the fourth scroll of every keyword.
        already_known = bool(step_ids) and all(
            (verdict_index is not None and verdict_index.has(ad_id))
            or run_seen.has(ad_id)
            for ad_id in step_ids
        )
        # Remember what a sibling had claimed, then publish ours.
        sibling_claimed.update(ad_id for ad_id in step_ids if run_seen.has(ad_id))
        run_seen.add_many(step_ids)

        if new_ads:
            quiet_steps = 0
            keyword_ads.extend(new_ads)
            # Yield that is entirely already-decided cannot improve the run, so
            # track it separately from "no cards at all".
            all_known_steps = all_known_steps + 1 if already_known else 0
        else:
            quiet_steps += 1

        # Early exit: this keyword is only turning up ads we already have a
        # verdict on, so scrolling to target just burns minutes. Requires a few
        # consecutive such steps so a single overlap burst cannot abandon a
        # keyword that still has new ads deeper down.
        if new_ads and all_known_steps >= ALL_KNOWN_STEPS_BEFORE_EXIT:
            logger.info(
                "%s⏭️ Keyword %r: %d consecutive steps of already-known ads — stopping early",
                tag, keyword, all_known_steps,
            )
            break

        if target_unique is None:
            quiet_budget = QUIET_STEPS_BEFORE_TARGET  # no estimate: trust only yield
        elif len(seen_ids) >= target_unique:
            logger.info("%s🏁 Safety margin hit: %d/%d unique (target %d) — done",
                        tag, len(seen_ids), estimate, target_unique)
            break
        else:
            quiet_budget = QUIET_STEPS_BEFORE_TARGET

        if quiet_steps >= quiet_budget:
            if target_unique is not None:
                logger.info("%s🛑 %d quiet steps with %d/%d unique — stopping",
                            tag, quiet_steps, len(seen_ids), estimate)
            break

        await scroll_step(page)

    if not keyword_ads and not empty_page:
        # Nothing matched the selectors — dump the results page so the card
        # selectors can be tuned against the real DOM. (A Meta empty state is a
        # KNOWN condition, not a bug — no dump.)
        await collect_page_html(page, tag=f"no_cards_{keyword.replace(' ', '_')}")

    fresh: list[AdRecord] = []
    already_final = deferred_renewed = heuristic_passed = 0
    for ad in keyword_ads:
        # In-memory check — the mirror already holds everything judged for this
        # family, everything recorded earlier in this run, and every ancestor's
        # verdicts. `skip` is request-aware: a deferred ad comes back for
        # judgement when the request family changes, and is only passed over
        # when it has not.
        if verdict_index is not None:
            skip, why = verdict_index.skip(ad.ad_archive_id, request_family_id)
            if skip:
                already_final += 1
                if why == "deferred":
                    # Passing over a deferred ad renews it — the whole cost of
                    # not re-judging it under an unchanged request.
                    if renew_deferral(family_id, ad.ad_archive_id):
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
        "%s🧺 Keyword %r: %d unique harvested (%d new this keyword) — %d already judged%s, "
        "%d passed heuristics -> %d to AI",
        tag, keyword, len(seen_ids), len(seen_ids) - keyword_start_unique, already_final,
        f" ({deferred_renewed} deferrals renewed)" if deferred_renewed else "",
        heuristic_passed, len(fresh),
    )
    return fresh


def _chunk(items: list[str], buckets: int) -> list[list[str]]:
    """Round-robin the keywords so every worker gets a similar-sized slice."""
    buckets = max(1, min(buckets, len(items)))
    out: list[list[str]] = [[] for _ in range(buckets)]
    for i, item in enumerate(items):
        out[i % buckets].append(item)
    return [chunk for chunk in out if chunk]


async def _scrape_on(
    context: BrowserContext,
    filters: ScrapingFilters,
    category,
    cat_id: str,
    run_seen: RunSeenIds,
    verdict_index=None,
    family_id: str | None = None,
    request_family_id: str | None = None,
    parallel_workers: int = 1,
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
    extract_lock = asyncio.Lock()

    workers = max(1, min(int(parallel_workers or 1), len(keywords)))
    if workers == 1:
        logger.info("🔁 Scraping %d keyword(s) sequentially", len(keywords))
        page = await context.new_page()
        try:
            fresh: list[AdRecord] = []
            for idx, keyword in enumerate(keywords):
                fresh.extend(await _scrape_keyword(
                    page, keyword, filters, category, cat_id, run_seen, verdict_index,
                    family_id, request_family_id, seen_ids, extract_lock,
                    is_first_keyword=(idx == 0),
                ))
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
                    out.extend(await _scrape_keyword(
                        pages[idx], keyword, filters, category, cat_id, run_seen,
                        verdict_index, family_id, request_family_id, seen_ids,
                        extract_lock, is_first_keyword=(idx == 0 and j == 0),
                        worker_label=label,
                    ))
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
