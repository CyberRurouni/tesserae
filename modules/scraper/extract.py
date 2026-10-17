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

v3: the whole page is read in ONE call instead of one call per card.

    The previous version walked every Library-ID span on the page and asked
    the browser to read its text — one round trip per card, every scroll
    step — and only afterwards checked whether it already knew the answer.
    So a page holding 250 cards cost 250 round trips to re-learn 240 ids it
    had been told minutes earlier. Measured on a real crawl: a keyword with
    153 ads made 20,494 reads where 153 would have done, 134x the
    necessary work — and because the DOM grows as you scroll, the waste
    compounds until a single pass takes minutes. That is the "it freezes on
    keywords with lots of results" symptom.

    The reader below is now one function that walks the page itself, skips
    any card whose id we already hold, and returns everything it found in
    one answer. Same selectors, same fields, same rules — only the number
    of round trips changed. Heuristic-only, no AI, still one pass per
    scroll step.

    Measured on live Meta, 6 scroll steps, same page, both readers:

        old 36.6s total   new 0.1s total   310x faster

    and on a synthetic 250-card page, the shape that freezes hardest:

        old 34.8s per pass   new 0.064s per pass   542x faster

    One behaviour did change, and it was a bug being fixed rather than a rule
    being altered: cta_text and cta_url are now populated. They were always
    None in production, because the CTA lookup used a bare `a[...]` XPath that
    matched nothing (see CTA_XPATH below). Verified live — the old form
    matched 0 anchors per card, the corrected descendant form matches the real
    buttons. Ads will now carry a call-to-action where they previously showed
    none, and the judge can finally see it.
"""

import logging
import re
from datetime import datetime, timezone

from playwright.async_api import Page

from core import AdRecord

from modules.fingerprint import ad_fingerprint

logger = logging.getLogger(__name__)

# ============================================================================
# 🔹 SELECTORS — stable anchors confirmed from a live dump
# ============================================================================

# The reader below carries its own copy of this pattern because it runs inside
# the page. This one is here for search.py, which asks the same question the
# cheap way — "how many cards are on screen" — to size the scroll.
LIBRARY_ID_SPAN_PATTERN = r"Library ID:\s*(\d+)"
CARD_XPATH = "ancestor::div[.//*[contains(@class, '_8nsi')]][1]"

CREATIVE = "._8nsi"                                   # identity block only (logo + advertiser)
ADVERTISER_LINK = f"{CREATIVE} a[href^='https://www.facebook.com/']"

# Ad body + CTA are SIBLINGS of _8nsi — searched at CARD level.
AD_TEXT_DIV = "div[style*='white-space: pre-wrap']"
# Creative CTA anchor: the l.facebook.com redirect, minus links that live
# INSIDE the ad text div (legacy class _7jyr) — those are body links.
#
# Two things a live dump settled, both now load-bearing:
#
#   * A BARE `a[...]` XPath finds nothing here. Playwright resolves a chained
#     xpath against the parent element, and `a` only matches direct children —
#     while Meta's anchors sit five or six divs down. Measured on a live card:
#     `a[...]` matched 0, `.//a[...]` matched 8. So cta_text and cta_url have
#     been silently blank in production for the life of this file. The reader
#     below uses a CSS descendant search, which does reach them.
#   * `_7jy-` is the CAROUSEL TILE wrapper and must NOT be excluded — each tile
#     carries its own button. Only `_7jyr` marks the ad body, confirmed live:
#     the in-text links sit inside `_7jyr _a25-` with a pre-wrap div above
#     them, while every real "Watch More"/"Learn more" sits outside it.
CTA_XPATH = ("xpath=.//a[contains(@href,'l.facebook.com')]"
             "[not(ancestor::div[contains(@class,'_7jyr')])]")
CTA_BUTTON_SPAN = "div.x2lah0s span"

STARTED_PATTERN = r"Started running on (.+)"
TOTAL_ADS_PATTERN = r"(\d+)\s+ads?\s+use this creative"
MULTIPLE_VERSIONS_TEXT = "This ad has multiple versions"
ACTIVE_TEXT = "Active"

_DATE_FORMATS = ("%d %b %Y", "%B %d, %Y", "%d %B %Y")

# ============================================================================
# 🔹 THE READER — runs inside the page
# ============================================================================
#
# One call per scroll step. It does the card walk, the "is this id new?"
# test, and the field extraction together, because every boundary between
# those steps used to cost a round trip.
#
# It returns TWO things on purpose:
#   ids      — every card on the page, known or not. This is progress: how far
#              down the list we have actually scrolled.
#   records  — full detail, for the cards we did NOT already hold. Asking the
#              browser about a card we are going to discard is the waste this
#              whole change exists to remove.
#
# `known` comes from Python and is used to skip work in the page, where a set
# lookup is effectively free, instead of shipping every known card's detail
# back over the wire.
_CARD_FIELDS_JS = r"""
(cfg) => {
  const ID_RE = /Library ID:\s*(\d+)/;
  const STARTED_RE = /Started running on (.+)/;
  const TOTAL_RE = /(\d+)\s+ads?\s+use this creative/i;
  const MULTI = 'This ad has multiple versions';

  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const body = (el) => (el ? (el.innerText || el.textContent || '') : '');
  const trim = (s) => (s || '').trim();

  const known = new Set(cfg.known || []);
  const ids = [];
  const records = [];
  const claimed = new Set();

  const spans = Array.from(document.querySelectorAll('span'))
    .filter((s) => ID_RE.test(s.textContent || ''));

  for (const span of spans) {
    const m = ID_RE.exec(span.textContent || '');
    if (!m) continue;
    const id = m[1];
    ids.push(id);
    if (known.has(id)) continue;

    // The card root: nearest ancestor DIV holding an _8nsi descendant. More
    // than one span can match (a span wrapping the real one), so `claimed`
    // makes sure a card is described once no matter how many anchors it has.
    let card = span.parentElement;
    while (card && !(card.tagName === 'DIV' && card.querySelector('[class*="_8nsi"]'))) {
      card = card.parentElement;
    }
    if (!card || claimed.has(card)) continue;
    claimed.add(card);

    let advertiser_name = null;
    let advertiser_page_url = null;
    const adv = card.querySelector('._8nsi a[href^="https://www.facebook.com/"]');
    if (adv) {
      advertiser_name = trim(body(adv)) || null;
      advertiser_page_url = adv.getAttribute('href');
    }

    // Carousel tiles each carry their own pre-wrap div, often whitespace
    // only — the longest non-empty text wins.
    let ad_text = '';
    for (const b of card.querySelectorAll('div[style*="white-space: pre-wrap"]')) {
      const t = trim(body(b));
      if (t.length > ad_text.length) ad_text = t;
    }

    let cta_text = null;
    let cta_url = null;
    const ctas = Array.from(card.querySelectorAll('a[href*="l.facebook.com"]'))
      .filter((a) => !a.closest('div[class*="_7jyr"]'));
    const cta = ctas.length ? ctas[ctas.length - 1] : null;
    if (cta) {
      cta_url = cta.getAttribute('href');
      const btn = cta.querySelector('div.x2lah0s span');
      cta_text = trim(body(btn)) || null;
      if (!cta_text) {
        const lines = body(cta).split('\n').map((l) => l.trim()).filter(Boolean);
        cta_text = lines.length ? lines[lines.length - 1] : null;
      }
    }

    let started_raw = null;
    for (const s of card.querySelectorAll('span')) {
      const sm = STARTED_RE.exec(s.textContent || '');
      if (sm) { started_raw = sm[1].trim().replace(/\.$/, ''); break; }
    }

    let is_active = false;
    for (const el of card.querySelectorAll('*')) {
      if (norm(el.textContent) === 'Active') { is_active = true; break; }
    }

    let total_active_ads = null;
    for (const s of card.querySelectorAll('span')) {
      const tm = TOTAL_RE.exec(s.textContent || '');
      if (tm) { total_active_ads = parseInt(tm[1], 10); break; }
    }

    const labels = [];
    if (card.querySelector('div[data-testid="ad-library-ad-carousel-container"]')) labels.push('carousel');
    if (card.querySelector('[data-testid="ad-content-body-video-container"]')) labels.push('video');
    for (const el of card.querySelectorAll('*')) {
      if (norm(el.textContent) === MULTI) { labels.push('multiple_versions'); break; }
    }

    records.push({
      ad_archive_id: id, advertiser_name, advertiser_page_url,
      ad_text, cta_text, cta_url, started_raw, is_active, total_active_ads, labels,
    });
  }
  return { ids, records };
}
"""


def _parse_started(raw: str | None) -> datetime | None:
    """'Started running on 26 Aug 2026' -> datetime (live-confirmed format)."""
    if not raw:
        return None
    m = re.search(STARTED_PATTERN, raw)
    raw_date = m.group(1).strip().rstrip(".") if m else raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw_date, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def extract_ads_from_page(
    page: Page,
    source_keyword: str,
    seen_ids: set[str] | None = None,
    seen_fingerprints: set[str] | None = None,
    encountered: set[str] | None = None,
) -> list[AdRecord]:
    """
    Read the ad cards currently in the page into AdRecords.

    New ones only, when the caller supplies `seen_ids`. Heuristic-only — no AI
    here. Cards are anchored on their Library ID span, so extraction survives
    Meta's class-name churn.

    `seen_fingerprints` collapses the same creative published under several
    Library IDs, which is what Meta does for one ad run across platforms.
    Checked AFTER the id, because the id is what decides "already harvested
    this run" and the fingerprint decides "this is the same ad wearing a
    different number".

    `encountered` receives EVERY id on the page, including ones already
    harvested — it is the caller's progress counter for one keyword: "how far
    down this list have we scrolled". It is deliberately not the same thing as
    the returned records, because reaching the bottom of a list and finding
    something new in it are two different questions, and only the page can
    answer the first one.
    """
    records: list[AdRecord] = []
    seen = seen_ids if seen_ids is not None else set()
    seen_prints = seen_fingerprints if seen_fingerprints is not None else set()
    collapsed = 0

    try:
        payload = await page.evaluate(_CARD_FIELDS_JS, {"known": sorted(seen)})
    except Exception as exc:  # noqa: BLE001 - a read failure must not kill the crawl
        logger.warning("⚠️ Could not read ad cards (%s)", exc)
        return []

    on_screen = payload.get("ids") or []
    if encountered is not None:
        encountered.update(on_screen)

    rows = payload.get("records") or []
    logger.info("🔍 %d ad card(s) in DOM for keyword %r (%d new)", len(on_screen), source_keyword, len(rows))

    for row in rows:
        try:
            ad_id = str(row.get("ad_archive_id") or "").strip()
            if not ad_id or ad_id in seen:
                continue
            seen.add(ad_id)

            started = _parse_started(row.get("started_raw"))
            record = AdRecord(
                ad_archive_id=ad_id,
                advertiser_name=row.get("advertiser_name") or None,
                advertiser_page_url=row.get("advertiser_page_url") or None,
                total_active_ads=row.get("total_active_ads"),
                ad_text=row.get("ad_text") or "",
                cta_text=row.get("cta_text") or None,
                cta_url=row.get("cta_url") or None,
                started_date=started,
                is_active=row.get("is_active"),
                ad_age_hours=(
                    (datetime.now(timezone.utc) - started).total_seconds() / 3600
                    if started
                    else None
                ),
                source_keyword=source_keyword,
                heuristic_labels=list(row.get("labels") or []),
            )
            # Same creative under a different Library ID — Meta does this for one
            # ad run across platforms. Kept out of the results, because five
            # rows of one ad is five times the reading for the user and five
            # times the AI cost, and the user already saw the first one.
            fingerprint = ad_fingerprint(record.advertiser_name, record.ad_text)
            if fingerprint and fingerprint in seen_prints:
                collapsed += 1
                continue
            if fingerprint:
                seen_prints.add(fingerprint)
                record.ad_fingerprint = fingerprint
            records.append(record)
        except Exception as e:  # noqa: BLE001
            logger.debug("⚠️ Skipped a card for keyword %r: %s", source_keyword, e)

    logger.info(
        "✅ Extracted %d new card(s) this pass (total unique: %d%s) for keyword %r",
        len(records), len(seen),
        f", {collapsed} duplicate creative(s) collapsed" if collapsed else "",
        source_keyword,
    )
    return records