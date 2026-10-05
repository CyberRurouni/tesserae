"""
main.py — the citizens of the scraper city.

One job: turn a RunConfig's filters into fresh, deduplicated AdRecords.
Everything heavier (browser opening, filter clicks, card extraction) is
packaged in this city's warehouses (browser.py / search.py / extract.py).
"""

import logging

from playwright.async_api import BrowserContext

from core import (
    AdRecord,
    CATEGORIES,
    ScrapingFilters,
    safe_redis_operation,
    verdict_broker,
    compute_profile_hashes,
    PROFILE_HASH_TTL,
)

from .browser import browser_session
from .extract import extract_ads_from_page
from .seen import RunSeenIds, get_seen_index
from .search import (
    apply_filters,
    collect_page_html,
    empty_results,
    read_results_estimate,
    scroll_step,
    search_keyword,
)

logger = logging.getLogger(__name__)

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

    if context is not None:
        return await _scrape_on(context, filters, category, cat_id, run_seen)
    async with browser_session(headless=headless) as owned:
        return await _scrape_on(owned, filters, category, cat_id, run_seen)


async def _scrape_on(
    context: BrowserContext,
    filters: ScrapingFilters,
    category,
    cat_id: str,
    run_seen: RunSeenIds,
) -> list[AdRecord]:
    """The keyword loop on an existing context. Owns only its page."""
    fresh: list[AdRecord] = []
    # Shared across EVERY keyword in this pass. One ad usually matches several
    # keywords (especially with an overlapping keyword set), and re-harvesting
    # it per keyword is what produced duplicate rows in the run files: the old
    # code rebuilt this set inside the loop, so dedup only ever applied within a
    # single keyword's scroll.
    seen_ids: set[str] = set()
    # Ads that already have a FINAL verdict under this category+profile. Loaded
    # once into memory rather than asked per card — a Redis round trip per
    # scraped card costs far more than the dedup it enables.
    seen_index = get_seen_index(cat_id, filters)
    # Ids looked at during THIS run, shared with any other worker on it.
    # Supplied by the caller (the orchestrator owns it for the whole run); the
    # smoke runner and tests may pass a memory-only instance.
    all_known_steps = 0

    page = await context.new_page()
    try:

        for idx, keyword in enumerate(filters.keywords):
            logger.info("▶️ Keyword: %r", keyword)
            # One navigation per keyword — the crafted search URL is the
            # whole trip. Cookie dismissal rides on the first search page;
            # no separate base-page round-trip.
            await search_keyword(page, keyword, filters, dismiss_cookies=(idx == 0))
            await apply_filters(page, filters)

            # Estimate-aware scroll+extract: read Meta's own '~N results'
            # header, then crawl until 80% of it is uniquely harvested
            # (safety margin), ending on quiet-step budgets that differ
            # by phase — latency-tolerant (10) before target, prompt (3)
            # after. Extraction yield, not scrollHeight, signals progress.
            keyword_start_unique = len(seen_ids)
            keyword_ads: list[AdRecord] = []
            all_known_steps = 0  # per keyword — never carried across the loop

            # Meta's own zero-results signal: skip the entire crawl —
            # scrolling an empty page is pointless and looks robotic.
            empty_page = await empty_results(page)
            if empty_page:
                logger.info("🚫 No ads match %r — skipping crawl", keyword)
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
                "🎯 Crawl plan: estimate~%s, target %s unique, quiet budget %s, cap %d steps",
                estimate if estimate is not None else "?",
                target_unique if target_unique is not None else "?",
                quiet_budget if estimate is not None else QUIET_STEPS_BEFORE_TARGET,
                hard_steps,
            )

            while steps < hard_steps:
                steps += 1
                new_ads = await extract_ads_from_page(
                    page, source_keyword=keyword, seen_ids=seen_ids
                )
                # Mirror this step's ids to the run-scoped set in one round
                # trip so sibling workers never re-harvest what we just took.
                run_seen.add_many(ad.ad_archive_id for ad in new_ads)

                if new_ads:
                    quiet_steps = 0
                    keyword_ads.extend(new_ads)
                    # Yield that is entirely already-final cannot improve the
                    # run, so track it separately from "no cards at all".
                    if all(
                        seen_index.has(ad.ad_archive_id) or run_seen.has(ad.ad_archive_id)
                        for ad in new_ads
                    ):
                        all_known_steps += 1
                    else:
                        all_known_steps = 0
                else:
                    quiet_steps += 1

                # Early exit: this keyword is only turning up ads we already
                # have a verdict on, so scrolling to target just burns minutes.
                # Requires a few consecutive such steps so a single overlap
                # burst can't abandon a keyword that still has new ads deeper
                # down the list.
                if new_ads and all_known_steps >= ALL_KNOWN_STEPS_BEFORE_EXIT:
                    logger.info(
                        "⏭️ Keyword %r: %d consecutive steps of already-known ads — stopping early",
                        keyword, all_known_steps,
                    )
                    break

                if target_unique is None:
                    quiet_budget = QUIET_STEPS_BEFORE_TARGET  # no estimate: trust only yield
                elif len(seen_ids) >= target_unique:
                    logger.info(
                        "🏁 Safety margin hit: %d/%d unique (target %d) — done",
                        len(seen_ids), estimate, target_unique,
                    )
                    break
                else:
                    quiet_budget = QUIET_STEPS_BEFORE_TARGET

                if quiet_steps >= quiet_budget:
                    if target_unique is not None:
                        logger.info(
                            "🛑 %d quiet steps with %d/%d unique — stopping",
                            quiet_steps, len(seen_ids), estimate,
                        )
                    break

                await scroll_step(page)

            if not keyword_ads and not empty_page:
                # Nothing matched the selectors — dump the results page so
                # the card selectors can be tuned against the real DOM.
                # (A Meta empty state is a KNOWN condition, not a bug — no dump.)
                await collect_page_html(page, tag=f"no_cards_{keyword.replace(' ', '_')}")

            already_final = heuristic_passed = 0
            fresh_before = len(fresh)
            for ad in keyword_ads:
                # In-memory check — the mirror already contains everything
                # marked earlier in this run and every previous run's verdicts.
                if seen_index.has(ad.ad_archive_id):
                    already_final += 1
                    continue
                if run_seen.has(ad.ad_archive_id) and run_seen.shared:
                    # Another worker on this run already took it.
                    already_final += 1
                    continue
                if not passes_heuristics(ad, filters, category):
                    continue
                heuristic_passed += 1
                ad.category = cat_id  # stamp the lens — AI + storage read it
                fresh.append(ad)

            logger.info(
                "🧺 Keyword %r: %d unique harvested (%d new this keyword) — %d already final, %d passed heuristics -> %d to AI (total: %d)",
                keyword,
                len(seen_ids),
                len(seen_ids) - keyword_start_unique,
                already_final,
                heuristic_passed,
                len(fresh) - fresh_before,
                len(fresh),
            )
    finally:
        await page.close()

    logger.info("✅ Scrape complete: %d fresh ads across %d keywords",
                len(fresh), len(filters.keywords))
    return fresh
