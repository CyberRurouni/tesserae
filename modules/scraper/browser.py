"""
browser.py — the warehouse: opens the browser, packages a page for citizens.

Uses Playwright's persistent context (option b): a dedicated profile folder
under `browser_profile/`. The user logs into Google once inside this profile;
cookies and session survive across runs, headless or not. The user's daily
Chrome is never touched.

Later, when Meta starts aggressively gating the library, this same file is
the single place that would grow a CDP connect-to-real-Chrome path —
citizens above won't notice the difference.
"""

import logging

from playwright.async_api import (
    BrowserContext,
    Playwright,
    async_playwright,
)

from core import BROWSER_PROFILE_DIR

logger = logging.getLogger(__name__)

ADS_LIBRARY_URL = "https://www.facebook.com/ads/library/"

# A real, common desktop Chrome on Windows — the library serves the full UI
# to this shape, and consistent fingerprints keep the logged-in session healthy.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


async def open_browser(headless: bool = False) -> tuple[Playwright, BrowserContext]:
    """
    Launch Chromium with the persistent Tesserae profile.

    Returns (playwright, context) — callers own BOTH and must pass both to
    close_browser. (Playwright's Browser object does not expose its driver,
    so the reference has to be carried explicitly.)
    """
    playwright: Playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_PROFILE_DIR),
        headless=headless,
        viewport={"width": 1366, "height": 900},
        user_agent=USER_AGENT,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )
    logger.info("✅ Browser opened (profile=%s, headless=%s)", BROWSER_PROFILE_DIR, headless)
    return playwright, context


async def close_browser(playwright: Playwright, context: BrowserContext) -> None:
    """Close the context and stop the underlying playwright driver."""
    await context.close()
    await playwright.stop()
    logger.info("🔒 Browser closed")


class browser_session:
    """Async context manager: `async with browser_session() as page:`"""

    def __init__(self, headless: bool = False):
        self._headless = headless
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> BrowserContext:
        self._playwright, self._context = await open_browser(self._headless)
        return self._context

    async def __aexit__(self, *exc) -> None:
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("🔒 Browser closed")
