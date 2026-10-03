"""
search.py — the route planners: turn ScrapingFilters into real interactions.

Strategy (learned from live runs): Meta's Ad Library is FULLY URL-driven —
country, category, media type, keyword query, and even date ranges are all
query parameters on the same URL. So the primary path never touches the
country/category pickers at all: we navigate directly to a crafted search
URL. The UI gate (country picker -> "All ads") is kept only as a
self-healing fallback: if Meta ever lands us on the gate anyway, we clear
it via the UI once, then re-navigate.

Fallback UI flow, per the Meta UI reference doc:
  Step 1 — Country: single-select. "Search for country" box, pinned
           "Current location", then a list starting with "All".
  Step 2 — Ad category: strict binary — "All ads" OR politics. Always "All ads".
  Step 3 — Keyword search: the main bar's placeholder reads "Choose an ad
           category" (misleadingly) — it is the keyword/advertiser search.

Pure browser mechanics — no extraction (extract.py), no judgement
(checkpoints city).
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import Locator, Page

from core import ScrapingFilters, Platform, MediaType

from .browser import ADS_LIBRARY_URL

logger = logging.getLogger(__name__)

DEBUG_DIR = Path(__file__).resolve().parents[2] / "debug"

# ============================================================================
# 🔹 SELECTORS / UI HOOKS — used by the fallback gate flow only.
# Confirmed from a live gate-page dump:
#   - Gate = two div[role='combobox'] dropdowns + a DISABLED main search
#     input (placeholder "Choose an ad category", literal disabled attr).
#   - ⚠️ The RESULTS page also renders both comboboxes in its header —
#     combobox count proves NOTHING. The search input's disabled state
#     is the ONLY gate fingerprint. (A combobox-count fallback misfired
#     on live results and re-ran the whole gate flow there.)
#   - Results page (post-gate, live-confirmed 2026-09-29): the main
#     search input's placeholder is "Search by keyword or advertiser" —
#     NOT the gate's "Choose an ad category". Results-ready checks must
#     accept the results placeholder or a rendered ad card.
#   - EMPTY RESULTS (live HTML, 2026-09-29): keywords with zero matches
#     render a full-page empty state — heading "No ads match your search
#     criteria" + "Clear Filters" button + the archive empty-state
#     image. Scrolling such a page is pointless and looks robotic.
#   - Country combobox is FIRST (label shows current country, e.g.
#     "Pakistan"); category combobox is SECOND (label "Ad category").
#   - A combobox's "Search for country" input only exists while the
#     dropdown is OPEN — never use it to detect the gate.
#   - Dropdown rows are plain text divs — no role='option' anywhere.
# ============================================================================

GATE_COMBOBOX = "div[role='combobox'][aria-haspopup='listbox']"
DISABLED_SEARCH_INPUT = "input[placeholder='Choose an ad category'][disabled]"
MAIN_SEARCH_INPUT = "input[placeholder='Choose an ad category']"

# Results page (post-gate) search input — different placeholder than the gate.
RESULTS_SEARCH_INPUT = "input[placeholder='Search by keyword or advertiser']"
# Meta's rough result-count header, e.g. '~78 results' / '~1,012 results'
# (role=heading, live-confirmed in the results dump).
RESULTS_ESTIMATE_PATTERN = re.compile(r"~?\s*([\d,]+)\s+results?", re.I)
# Enabled search input under EITHER placeholder — gate-cleared success signal.
ENABLED_SEARCH_INPUT = (
    "input[placeholder='Search by keyword or advertiser']:not([disabled]), "
    "input[placeholder='Choose an ad category']:not([disabled])"
)
COUNTRY_SEARCH_INPUT = "input[placeholder='Search for country']"
CATEGORY_LABEL_TEXT = "All ads"
COUNTRY_ALL_TEXT = "All"

# Legacy hooks kept as fallbacks — harmless if absent.
COOKIE_DISMISS = (
    "button:has-text('Allow all cookies'), "
    "button:has-text('Allow essential cookies')"
)
RESULTS_CONTAINER = "[role='main']"


async def _first_visible(page: Page, selector: str, timeout_ms: int = 3000) -> Locator | None:
    """First visible match of a selector, or None — never raises."""
    try:
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=timeout_ms)
        return loc
    except Exception:
        return None


async def _dump_debug(page: Page, tag: str) -> None:
    """Screenshot + HTML snapshot of the current page into debug/."""
    DEBUG_DIR.mkdir(exist_ok=True)
    try:
        await page.screenshot(path=str(DEBUG_DIR / f"{tag}.png"), full_page=True)
        (DEBUG_DIR / f"{tag}.html").write_text(await page.content(), encoding="utf-8")
        logger.warning("🧪 Debug artifacts dumped: %s.png/.html in %s", tag, DEBUG_DIR)
    except Exception as e:
        logger.error("🧪 Debug dump failed: %s", e)


# ============================================================================
# 🔹 URL CONSTRUCTION — the primary search path
# ============================================================================

def build_search_url(keyword: str, filters: ScrapingFilters) -> str:
    """
    Craft the library search URL directly. Params confirmed from live
    redirect URLs; ad-age becomes a server-side start_date floor.
    """
    regions = filters.delivered_to_regions
    if len(regions) == 1:
        country: str = regions[0]
    else:
        if len(regions) > 1:
            logger.warning(
                "⚠️ %d regions requested but Meta's country filter is "
                "single-select — using ALL. Narrow per-run instead.",
                len(regions),
            )
        country = "ALL"

    params: dict[str, str] = {
        "active_status": filters.delivery_status.value,   # all | active | inactive
        "ad_type": "all",                                 # category = "All ads"
        "country": country,
        "is_targeted_country": "false",
        "media_type": filters.media_type.value,
        "q": keyword,
        "search_type": "keyword_unordered",
    }

    # Add language filter (Meta uses content_languages[0]=en format)
    if filters.languages:
        lang_values = [l.strip().lower() for l in filters.languages if l.strip()]
        # One param per language so every selected language is honoured
        for index, lang in enumerate(lang_values):
            params[f"content_languages[{index}]"] = lang

    # Add platform filter (Meta uses publisher_platforms[0]=instagram format)
    if filters.platforms:
        plat_values = [p.value for p in filters.platforms]
        all_plat_values = [p.value for p in Platform]
        if plat_values != all_plat_values:  # not "all"
            # One param per platform so every checked platform is searched
            for index, platform in enumerate(plat_values):
                params[f"publisher_platforms[{index}]"] = platform

    # Add sort parameters
    if filters.sort_order == "date":
        params["sort_data[mode]"] = "total_impressions"
        params["sort_data[direction]"] = "desc"
    elif filters.sort_order == "relevance":
        params["sort_data[mode]"] = "relevance"
        params["sort_data[direction]"] = "desc"

    if filters.ad_age_hours is not None:
        floor = datetime.now(timezone.utc) - timedelta(hours=filters.ad_age_hours)
        params["start_date[min]"] = floor.strftime("%Y-%m-%d")

    return f"{ADS_LIBRARY_URL}?{urlencode(params)}"


# ============================================================================
# 🔹 GATE FALLBACK — UI flow, only when the URL path lands on the gate
# ============================================================================

async def _dismiss_cookies(page: Page) -> None:
    """Dismiss the cookie banner if present (first load of a run, usually)."""
    if btn := await _first_visible(page, COOKIE_DISMISS, timeout_ms=2000):
        try:
            await btn.click()
            await page.wait_for_timeout(500)
            logger.info("🚪 Dismissed cookie banner")
        except Exception:
            pass


async def _click_visible_text(page: Page, text: str, exact: bool = True) -> bool:
    """Click the first VISIBLE element whose text is exactly `text`."""
    loc = page.get_by_text(text, exact=exact)
    try:
        count = await loc.count()
    except Exception:
        return False
    for i in range(count):
        el = loc.nth(i)
        try:
            if await el.is_visible():
                await el.click()
                await page.wait_for_timeout(800)
                return True
        except Exception:
            continue
    return False


async def gate_present(page: Page) -> bool:
    """
    True when the country/category gate is on screen.

    Fingerprint: the DISABLED main search input — nothing else. The
    results page carries the same two comboboxes in its header, so
    counting comboboxes misfires there (it used to re-run the entire
    gate flow on live results). A positive check is confirmed once more
    after a short pause, so a transient disabled state while the results
    page hydrates can't trigger the flow either.
    """
    for _ in range(2):
        if await _first_visible(page, DISABLED_SEARCH_INPUT, timeout_ms=1000) is None:
            return False
        await page.wait_for_timeout(600)
    logger.info("🌐 Gate detected (search input disabled)")
    return True


async def clear_gate_via_ui(page: Page) -> None:
    """
    Clear the country+category gate through the UI (doc sequence):
    1) country combobox -> 'All'   2) category combobox -> 'All ads'.
    Rows are plain text divs, so: click by exact visible text, with a
    keyboard fallback (ArrowDown+Enter on the open listbox).
    """
    combos = page.locator(GATE_COMBOBOX)

    # Step 1 — country
    if await combos.count() >= 1:
        await combos.nth(0).click()
        await page.wait_for_timeout(800)

        # The picker's own filter input exists only while open — use it to
        # narrow the list, then click the exact 'All' row.
        if picker_filter := await _first_visible(page, COUNTRY_SEARCH_INPUT, timeout_ms=2000):
            await picker_filter.fill("All")
            await page.wait_for_timeout(600)

        if await _click_visible_text(page, COUNTRY_ALL_TEXT):
            logger.info("✅ Country set to 'All'")
        else:
            await page.keyboard.press("ArrowDown")
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(800)
            logger.info("✅ Country set via keyboard fallback")
    else:
        logger.info("✅ Country combobox not shown — already set")

    # Step 2 — category
    if await combos.count() >= 2:
        await combos.nth(1).click()
        await page.wait_for_timeout(800)

        if await _click_visible_text(page, CATEGORY_LABEL_TEXT):
            logger.info("✅ Ad category set to 'All ads'")
        else:
            await page.keyboard.press("ArrowDown")
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(800)
            logger.info("✅ Ad category set via keyboard fallback")

        # Success signal: the search input is enabled under either placeholder.
        if await _first_visible(page, ENABLED_SEARCH_INPUT, timeout_ms=3000):
            logger.info("✅ Gate cleared — main search input is live")
    else:
        logger.info("✅ Category already set (no second combobox)")


# ============================================================================
# 🔹 SEARCH — navigate to the crafted URL; heal the gate if it appears
# ============================================================================

async def wait_for_results(page: Page, timeout_s: float = 40.0) -> bool:
    """
    Poll for a results-ready signal — ANY of:
      1. the search input is enabled under either placeholder (the results
         page uses "Search by keyword or advertiser", the gate page
         "Choose an ad category"), or
      2. the first ad card has rendered (a span reading "Library ID: ...").
    Returns True/False; never raises. On timeout, dumps debug artifacts
    so the stuck page is available for selector tuning.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await _first_visible(page, ENABLED_SEARCH_INPUT, timeout_ms=750):
            logger.info("✅ Results ready (search input is live)")
            return True
        try:
            if await page.get_by_text(re.compile(r"Library ID:\s*\d+")).count() > 0:
                logger.info("✅ Results ready (first ad card rendered)")
                return True
        except Exception:
            pass
        await page.wait_for_timeout(1000)
    await _dump_debug(page, "results_not_detected")
    logger.warning("⚠️ No results-ready signal within %.0fs — continuing anyway (extraction will report what's on the page)", timeout_s)
    return False


async def search_keyword(
    page: Page,
    keyword: str,
    filters: ScrapingFilters,
    *,
    dismiss_cookies: bool = False,
) -> None:
    """
    Run one keyword search by direct URL navigation — one navigation, no
    detours. If the real gate appears anyway, clear it via the UI once
    and retry the URL. Never raises on missing results — a zero-yield
    keyword is a legitimate outcome.
    """
    url = build_search_url(keyword, filters)
    logger.info("🔎 Searching %r via URL: %s", keyword, url)

    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)
    if dismiss_cookies:
        await _dismiss_cookies(page)

    if await gate_present(page):
        logger.info("🌐 Gate appeared despite URL params — clearing via UI")
        await clear_gate_via_ui(page)
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        logger.info("🔁 Retried search URL after gate clearance")

    await wait_for_results(page)
    await page.wait_for_timeout(1500)


async def apply_filters(page: Page, filters: ScrapingFilters) -> None:
    """
    Post-navigation filter staging. Date/ad-age and delivery status are
    already applied server-side via URL params (build_search_url). The
    remaining UI controls (platform, media type, advertiser) land in a
    second pass after live validation.
    """
    logger.info(
        "🎛️ Filters applied via URL: status=%s, ad_age_hours=%s, regions=%s",
        filters.delivery_status.value,
        filters.ad_age_hours,
        filters.delivered_to_regions or "ALL",
    )


# The heading of Meta's zero-results empty state (stable English text).
EMPTY_RESULTS_TEXT = "No ads match your search criteria"


async def empty_results(page: Page) -> bool:
    """
    True when Meta rendered its 'No ads match your search criteria'
    empty state — the caller should skip crawling this keyword entirely.
    Checked twice (700ms apart) so a hydration delay can't mask a real
    result set as empty.
    """
    for attempt in range(2):
        try:
            if await page.get_by_text(EMPTY_RESULTS_TEXT, exact=True).count() > 0:
                return True
        except Exception:
            pass  # detached mid-hydration — retry
        if attempt == 0:
            await page.wait_for_timeout(700)
    return False


async def read_results_estimate(page: Page, timeout_s: float = 8.0) -> int | None:
    """
    Read Meta's own rough result count ('~78 results') from the results
    header. Retries briefly while the page hydrates; returns None if no
    parseable count ever shows — the caller then falls back to a fixed
    scroll budget.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        try:
            loc = page.get_by_text(RESULTS_ESTIMATE_PATTERN).first
            if await loc.count():
                raw = (await loc.inner_text()).strip()
                if (m := RESULTS_ESTIMATE_PATTERN.search(raw)):
                    estimate = int(m.group(1).replace(",", ""))
                    logger.info("🔢 Meta reports ~%d results", estimate)
                    return estimate
        except Exception:
            pass  # detached mid-hydration — retry
        await page.wait_for_timeout(1000)
    logger.warning("⚠️ No result-count header found — proceeding without an estimate")
    return None


async def scroll_step(page: Page, distance: int = 4000, pause_s: float = 1.5) -> None:
    """
    One wheel step down the results pane, with a cooldown pause (keeps
    latency down and lets lazy-loaded cards render). The scroll+extract
    LOOP lives in run_scrape (main.py) — estimate-aware, driven by
    extraction yield, not scrollHeight.
    """
    await page.mouse.wheel(0, distance)
    await page.wait_for_timeout(int(pause_s * 1000))


async def collect_page_html(page: Page, tag: str = "results") -> str:
    """Dump the full page HTML into debug/ and return it — used to tune selectors against reality."""
    html = await page.content()
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        (DEBUG_DIR / f"{tag}.html").write_text(html, encoding="utf-8")
        logger.warning("🧪 Results HTML dumped: debug/%s.html", tag)
    except Exception as e:
        logger.error("🧪 Debug dump failed: %s", e)
    return html
