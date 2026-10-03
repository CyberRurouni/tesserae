"""
relevance.py — checkpoint (a): is this ad actually what the user wants?

Heuristics (substring patterns, ad age) are deliberately blunt — they're a
cheap first sieve. This gate is the precise one: Gemini reads each ad's
real content and decides whether it matches the user's intent. Today the
intent is fixed to job & career opportunities; tomorrow it becomes the
user's own written `looking_for` (UserProfile) — the function signature is
already shaped for that.

Contract:
  - The intent comes from each ad's CATEGORY (core.CATEGORIES) — the
    lens description. Ads of different categories in one call are
    grouped and judged against their own description.
  - Ads are packed into batches of ≤10 and sent to Gemini, with ≤5
    calls in flight at once (asyncio.Semaphore) — a big harvest waits
    its turn instead of hammering the API.
  - One verdict per ad, always. A failed/invalid model answer is a
    REJECT flagged ai_failed=True (fail-closed) — the caller can give
    those ads another chance on the next run, while model-judged
    rejections are final (per category: verdict keys are category-
    scoped, so another category's lens starts clean).
  - The WHOLE ad text goes to the model — no truncation. Judgement
    quality beats token frugality here.
  - No storage, no scraping — judgement only.
"""

import asyncio
import logging

from core import (
    AIClient,
    AdRelevanceVerdict,
    AdRecord,
    CATEGORIES,
    DEFAULT_CATEGORY_ID,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 10             # ads per Gemini call
MAX_CONCURRENT_BATCHES = 5  # calls in flight at once — extras queue up

_SYSTEM_PROMPT = """\
You are a strict ad-relevance judge in a lead-generation pipeline.

You receive a USER INTENT and a numbered list of ads (id, advertiser, \
text, CTA). For EVERY ad you decide: does this ad's actual content offer \
what the intent asks for?

Judging rules:
- Judge the ad's CONTENT, never the mere presence of intent keywords. \
An ad containing the word "hiring" is not enough — it must actually \
announce or offer the thing itself.
- Educational providers count ONLY when the specific ad announces real \
hiring (placement drives, recruitment by the provider) — never when it \
sells a course, bootcamp, certificate or webinar.
- Ads promoting software, agencies, services, events or content channels \
are not opportunities.
- Ads with no discernible offer, or offers unrelated to the intent, are \
not relevant.
- When genuinely unsure, answer relevant=false.

Answer with JSON only, exactly this shape, one entry per input id and \
no extra ids:
{"verdicts": [{"id": "<ad id>", "relevant": true|false, \
"confidence": <0.0-1.0>, "reason": "<max 12 words>"}]}
"""


def _normalize_confidence(raw) -> float:
    """Accept 0-1 or 0-100 (models slip between scales); clamp to [0, 1]."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.5
    if value > 1.0:
        value /= 100.0
    return min(max(value, 0.0), 1.0)


def _format_ad(ad: AdRecord, index: int) -> str:
    # Full ad text — the whole creative goes to the judge, no clipping.
    cta = f" | CTA: {ad.cta_text}" if ad.cta_text else ""
    return f"[{index}] id={ad.ad_archive_id} | advertiser={ad.advertiser_name or 'unknown'}{cta}\n{ad.ad_text}"


async def _judge_batch(
    client: AIClient,
    batch: list[AdRecord],
    batch_no: int,
    intent: str,
    additional_filters: str | None = None,
) -> dict[str, AdRelevanceVerdict]:
    """
    One Gemini call for up to BATCH_SIZE ads. Never raises: any failure
    rejects the whole batch (fail-closed) with an explanatory reason.
    """
    ids = [ad.ad_archive_id for ad in batch]
    listing = "\n\n".join(_format_ad(ad, i) for i, ad in enumerate(batch, start=1))
    user_prompt = (
        f"USER INTENT: {intent}\n\n"
        f"{len(batch)} ads to judge:\n\n{listing}"
    )
    if additional_filters:
        user_prompt += (
            "\n\nADDITIONAL USER FILTERS (hard constraints — an ad that "
            f"violates any of these is NOT relevant):\n{additional_filters}"
        )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        raw = await client.blocking(
            messages,
            max_tokens=2000,
            increment=400,
            temperature=0.0,
        )
    except Exception as e:
        logger.error("🚫 Relevance batch %d failed entirely — failing closed: %s", batch_no, e)
        return {
            ad_id: AdRelevanceVerdict(
                ad_archive_id=ad_id, relevant=False, confidence=0.0,
                reason="checkpoint error — will be retried next run",
                ai_failed=True,
            )
            for ad_id in ids
        }

    verdicts: dict[str, AdRelevanceVerdict] = {}
    rows = raw.get("verdicts") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        logger.error("🚫 Relevance batch %d returned malformed JSON — failing closed", batch_no)
        return {
            ad_id: AdRelevanceVerdict(
                ad_archive_id=ad_id, relevant=False, confidence=0.0,
                reason="malformed model output — will be retried next run",
                ai_failed=True,
            )
            for ad_id in ids
        }

    known = set(ids)
    for row in rows:
        if not isinstance(row, dict):
            continue
        ad_id = str(row.get("id", "")).strip()
        if ad_id not in known or ad_id in verdicts:
            continue  # hallucinated or duplicate id — ignore
        verdicts[ad_id] = AdRelevanceVerdict(
            ad_archive_id=ad_id,
            relevant=bool(row.get("relevant", False)),
            confidence=_normalize_confidence(row.get("confidence")),
            reason=str(row.get("reason", "")).strip(),
        )

    # Ads the model stayed silent about → reject (fail-closed), and
    # treated as a failure: the model was instructed to answer for EVERY
    # id, so silence is a protocol breach, not a judgement.
    for ad_id in ids:
        if ad_id not in verdicts:
            verdicts[ad_id] = AdRelevanceVerdict(
                ad_archive_id=ad_id, relevant=False, confidence=0.0,
                reason="no verdict returned — will be retried next run",
                ai_failed=True,
            )

    passed = sum(1 for v in verdicts.values() if v.relevant)
    logger.info("⚖️ Relevance batch %d: %d/%d passed", batch_no, passed, len(batch))
    return verdicts


async def judge_ad_relevance(
    ads: list[AdRecord],
    intent: str | None = None,
    batch_size: int = BATCH_SIZE,
    max_concurrent: int = MAX_CONCURRENT_BATCHES,
    additional_filters: str | None = None,
) -> dict[str, AdRelevanceVerdict]:
    """
    Checkpoint (a): judge every ad against its category's description
    (an explicit `intent` overrides, for experiments).

    Returns a verdict for EVERY input ad id (missing model answers are
    fail-closed rejects). Ads are grouped by category, packed into
    batches of `batch_size`, with at most `max_concurrent` calls in
    flight — the rest wait their turn.
    """
    if not ads:
        return {}

    # Defensive dedup — upstream already dedups, but verdicts must be unique.
    unique: dict[str, AdRecord] = {ad.ad_archive_id: ad for ad in ads}

    # Group by category: each lens judges against its own description.
    fallback_desc = CATEGORIES[DEFAULT_CATEGORY_ID].description
    by_category: dict[str, list[AdRecord]] = {}
    for ad in unique.values():
        by_category.setdefault(ad.category or DEFAULT_CATEGORY_ID, []).append(ad)

    batches: list[tuple[str, list[AdRecord]]] = []
    for cat_id, group in by_category.items():
        description = intent or (
            CATEGORIES[cat_id].description if cat_id in CATEGORIES else fallback_desc
        )
        for i in range(0, len(group), batch_size):
            batches.append((description, group[i : i + batch_size]))

    logger.info(
        "🎯 Checkpoint (a): judging %d ad(s) across %d category lens(es), "
        "%d batch(es) of ≤%d (≤%d calls in flight)",
        len(unique), len(by_category), len(batches), batch_size, max_concurrent,
    )

    client = AIClient()
    gate = asyncio.Semaphore(max_concurrent)

    async def _run(
        batch_no: int, description: str, batch: list[AdRecord]
    ) -> dict[str, AdRelevanceVerdict]:
        async with gate:
            return await _judge_batch(client, batch, batch_no, description, additional_filters)

    results = await asyncio.gather(
        *(
            _run(batch_no, description, batch)
            for batch_no, (description, batch) in enumerate(batches, start=1)
        )
    )

    verdicts: dict[str, AdRelevanceVerdict] = {}
    for batch_result in results:
        verdicts.update(batch_result)

    passed = [v for v in verdicts.values() if v.relevant]
    shaky = [v for v in passed if v.confidence < 0.6]
    failed = [v for v in verdicts.values() if v.ai_failed]
    logger.info(
        "✅ Checkpoint (a): %d/%d ads relevant%s%s",
        len(passed), len(verdicts),
        f" — {len(shaky)} of them low-confidence" if shaky else "",
        f" — {len(failed)} lost to AI failure (will retry next run)" if failed else "",
    )
    return verdicts
