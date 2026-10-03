"""
extract.py — the field hands: read ads off a rendered results page.

Selector strategy, v2 — built from a LIVE results-page dump (no more
guesses). Meta's obfuscated classes (x78zum5, x1plvlek, ...) churn between
deploys, but two anchors are stable:

  1. Every ad card contains exactly one span reading "Library ID: <digits>".
  2. Every card contains exactly one creative wrapper with legacy class
     "_8nsi" — underscore-prefixed legacy classes are NOT obfuscated
     (_8nsi, _7jyh, _7jyr, _8nqq, _8nrv, _4ik4/_4ik5).

So: anchor on the Library-ID span, walk up to the nearest ancestor div
that also contains an "_8nsi" element — that ancestor is the card.

v2.1 (live-verified): _8nsi is only the IDENTITY block (logo + advertiser
anchor). The ad body div (white-space: pre-wrap) and the creative CTA
anchor are SIBLINGS of _8nsi inside the creative wrapper — so ad text,
CTA and video are searched at CARD level, never inside _8nsi. In-text
l.facebook.com links (inside the pre-wrap div, legacy class _7jyr) are
excluded from the CTA search. Carousel tiles carry their own pre-wrap
divs, often whitespace-only — the longest non-empty text wins.

v2.2: extraction runs in INCREMENTAL passes — one pass per scroll step,
with the caller supplying `seen_ids` — because the virtualized DOM
recycles cards mid-scroll; a single slow pass misses most of them.
"""

import logging
import re
from datetime import datetime, timezone

from playwright.async_api import Page

from core import AdRecord

logger = logging.getLogger(__name__)

# ============================================================================
# 🔹 SELECTORS — stable anchors confirmed from a live dump
# ============================================================================

LIBRARY_ID_TEXT = re.compile(r"Library ID:\s*(\d+)")
CARD_XPATH = "ancestor::div[.//*[contains(@class, '_8nsi')]][1]"

CREATIVE = "._8nsi"                                   # identity block only (logo + advertiser)
ADVERTISER_LINK = f"{CREATIVE} a[href^='https://www.facebook.com/']"

# Ad body + CTA are SIBLINGS of _8nsi — searched at CARD level.
AD_TEXT_DIV = "div[style*='white-space: pre-wrap']"
# Creative CTA anchor: the l.facebook.com redirect, minus links that live
# INSIDE the ad text div (legacy class _7jyr) — those are body links.
CTA_XPATH = ("xpath=a[contains(@href,'l.facebook.com')]"
             "[not(ancestor::div[contains(@class,'_7jyr')])]")
CTA_BUTTON_SPAN = "div.x2lah0s span"

STARTED_PATTERN = re.compile(r"Started running on (.+)")
TOTAL_ADS_PATTERN = re.compile(r"(\d+)\s+ads?\s+use this creative", re.I)
MULTIPLE_VERSIONS_TEXT = "This ad has multiple versions"
ACTIVE_TEXT = "Active"

_DATE_FORMATS = ("%d %b %Y", "%B %d, %Y", "%d %B %Y")


def _parse_started(raw: str | None) -> datetime | None:
    """'Started running on 26 Aug 2026' -> datetime (live-confirmed format)."""
    if not raw:
        return None
    m = STARTED_PATTERN.search(raw)
    raw_date = m.group(1).strip().rstrip(".") if m else raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw_date, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def _text_of(locator, limit: int = 1) -> str | None:
    """inner_text of the Nth match (default first), or None — never raises."""
    try:
        if await locator.count() == 0:
            return None
        loc = locator.first if limit == 1 else locator.nth(limit - 1)
        text = (await loc.inner_text()).strip()
        return text or None
    except Exception:
        return None


async def extract_ads_from_page(
    page: Page,
    source_keyword: str,
    seen_ids: set[str] | None = None,
) -> list[AdRecord]:
    """
    Extract the ad cards currently in the DOM into AdRecords (new ones
    only, when the caller supplies `seen_ids`). Heuristic-only — no AI
    here. Cards are anchored on their Library ID span, so extraction
    survives Meta's class-name churn.
    """
    records: list[AdRecord] = []
    seen = seen_ids if seen_ids is not None else set()

    id_spans = page.locator("span").filter(has_text=re.compile(r"Library ID:\s*\d+"))
    count = await id_spans.count()
    logger.info("🔍 %d ad cards in DOM for keyword %r", count, source_keyword)

    for i in range(count):
        span = id_spans.nth(i)
        try:
            m = LIBRARY_ID_TEXT.search(await span.inner_text())
            if not m:
                continue
            ad_id = m.group(1)
            if ad_id in seen:  # earlier pass already harvested this card
                continue
            seen.add(ad_id)

            # Nearest ancestor containing the creative = the card root
            card = span.locator(f"xpath={CARD_XPATH}")

            advertiser_name = advertiser_url = None
            adv = card.locator(ADVERTISER_LINK).first
            if await adv.count():
                advertiser_name = (await adv.inner_text()).strip() or None
                advertiser_url = await adv.get_attribute("href")

            # Carousel tiles each carry their own pre-wrap div (often
            # whitespace-only) — take the LONGEST non-empty text, not the first.
            ad_text = ""
            try:
                for node in await card.locator(AD_TEXT_DIV).all():
                    chunk = (await node.inner_text()).strip()
                    if len(chunk) > len(ad_text):
                        ad_text = chunk
            except Exception:
                pass

            cta_text = cta_url = None
            cta = card.locator(CTA_XPATH).last  # creative CTA anchor (card-level)
            if await cta.count():
                cta_url = await cta.get_attribute("href")
                cta_text = await _text_of(cta.locator(CTA_BUTTON_SPAN))
                if not cta_text:  # fallback: last non-empty line of the link
                    lines = [ln.strip() for ln in (await cta.inner_text()).splitlines() if ln.strip()]
                    cta_text = lines[-1] if lines else None

            started = _parse_started(
                await _text_of(card.locator("span").filter(has_text=STARTED_PATTERN))
            )

            is_active = await card.get_by_text(ACTIVE_TEXT, exact=True).count() > 0

            total_active_ads = None
            ta_text = await _text_of(
                card.locator("span").filter(has_text=TOTAL_ADS_PATTERN)
            )
            if ta_text and (tm := TOTAL_ADS_PATTERN.search(ta_text)):
                total_active_ads = int(tm.group(1))

            labels: list[str] = []
            if await card.locator("div[data-testid='ad-library-ad-carousel-container']").count():
                labels.append("carousel")
            if await card.locator("[data-testid='ad-content-body-video-container']").count():
                labels.append("video")
            if await card.get_by_text(MULTIPLE_VERSIONS_TEXT).count():
                labels.append("multiple_versions")

            record = AdRecord(
                ad_archive_id=ad_id,
                advertiser_name=advertiser_name,
                advertiser_page_url=advertiser_url,
                total_active_ads=total_active_ads,
                ad_text=ad_text,
                cta_text=cta_text,
                cta_url=cta_url,
                started_date=started,
                is_active=is_active,
                ad_age_hours=(
                    (datetime.now(timezone.utc) - started).total_seconds() / 3600
                    if started
                    else None
                ),
                source_keyword=source_keyword,
                heuristic_labels=labels,
            )
            records.append(record)
        except Exception as e:
            logger.debug("⚠️ Skipped card %d for keyword %r: %s", i, source_keyword, e)

    logger.info(
        "✅ Extracted %d new cards this pass (total unique: %d) for keyword %r",
        len(records), len(seen), source_keyword,
    )
    return records
