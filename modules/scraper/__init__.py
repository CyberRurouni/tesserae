"""
Scraper city — browser automation against the Meta Ads Library.

Solves one problem of the country: turning ScrapingFilters into fresh,
deduplicated AdRecords, with zero AI involved.

Citizens (main.py) draw from the city's warehouses:
  - browser.py   — persistent Chromium profile (log in to Google once)
  - search.py    — navigation, search box, UI filters, lazy-load scrolling
  - extract.py   — ad-card -> AdRecord extraction (selectors isolated there)
"""

from .browser import browser_session, open_browser, close_browser
from .extract import extract_ads_from_page
from .main import already_fetched, mark_fetched, run_scrape
from .seen import RunSeenIds, SeenIndex, get_seen_index, reset_index_cache

__all__ = [
    "browser_session",
    "open_browser",
    "close_browser",
    "extract_ads_from_page",
    "already_fetched",
    "mark_fetched",
    "run_scrape",
    "RunSeenIds",
    "SeenIndex",
    "get_seen_index",
    "reset_index_cache",
]
