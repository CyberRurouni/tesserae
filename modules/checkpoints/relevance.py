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

You receive THREE separate inputs and a numbered list of ads. They play \
different roles and you must not confuse them:

  ABOUT YOU          who the person IS. Defines what fields are theirs at all.
  ADDITIONAL REQUEST what they want RIGHT NOW. Temporary scoping — it never \
                     changes who they are.
  CATEGORY           the kind of opportunity being searched for.

Decide in THREE stages, in this order, for every ad. Stop at the first stage \
that answers.

STAGE 1 — does this ad offer actual employment to anyone?
  NO  -> outcome "rejected", rejection_kind "no_hiring".
          Nobody is being employed: a course, bootcamp, certification, webinar, \
          B2B / recruiting / outsourcing / staffing service, job board, \
          marketplace, agency product, or content such as a podcast, talk or \
          newsletter. A company advertising that it recruits for OTHER companies \
          is a service provider, not an employer.
  YES -> go to stage 2.

STAGE 2 — is the role inside what this person actually does?
  NO  -> outcome "rejected", rejection_kind "not_for_me".
          An employer IS hiring, but not for them: a different field, a \
          different stack, unrelated seniority. This is specific to THIS \
          person — another profile might well want it, so it is never treated \
          as universally useless.
  YES -> go to stage 3.

STAGE 3 — is it wanted under the current request?
  YES -> outcome "accepted"
  NO  -> outcome "deferred". Right field, wrong moment. NOT trash: they may \
          want it under a different request.

The distinction between the two rejection kinds is the most important part of \
your answer. "no_hiring" is a permanent, global judgement about the ad itself. \
"not_for_me" is only true against this one profile. Getting them backwards \
either hides a good lead forever, or re-judges junk for every profile.

Judging rules:
- Judge the ad's CONTENT, never the mere presence of keywords. An ad \
containing the word "hiring" is not enough — it must actually offer a role.
- Seniority, contract vs permanent, and remote-vs-onsite are REQUEST \
questions, never profile questions: they can defer an ad, never reject it.
- Company location and employer nationality are REQUEST questions unless the \
ad offers no employment at all.
- When genuinely unsure at stage 1, answer "no_hiring".
- When genuinely unsure at stage 2, answer "not_for_me".

Also give each ad a short "category" naming the work it is about (e.g. \
"Backend development", "Data science").

Answer with JSON only, exactly this shape, one entry per input id and no \
extra ids:
{"verdicts": [{"id": "<ad id>", "outcome": "accepted|deferred|rejected", \
"rejection_kind": "no_hiring|not_for_me", "category": "<max 3 words>", \
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


def _parse_rejection_kind(row: dict) -> str:
    """
    Read the rejection kind, defaulting to the SAFE end.

    Anything unrecognised becomes "not_for_me". That only costs a re-judgement
    under another profile; calling something junk it isn't would discard it for
    good, which is the one irreversible decision in this system.
    """
    value = str(row.get("rejection_kind") or row.get("rejectionKind") or "").strip().lower()
    return "no_hiring" if value == "no_hiring" else "not_for_me"


def _parse_outcome(row: dict) -> str:
    """
    Read the outcome, tolerating a model that still answers in the old shape.

    An old-shaped reply is only trustworthy when it says yes (accept) or when
    there is no request at all (nothing could be merely deferred). With a
    request in play a bare "false" is ambiguous — the ad may well be deferred —
    so it is resolved by the rule below rather than guessed.
    """
    value = str(row.get("outcome") or "").strip().lower()
    if value in ("accepted", "deferred", "rejected"):
        return value
    if "relevant" in row:
        if row.get("relevant"):
            return "accepted"
        return "rejected" if not row.get("_has_request") else "deferred"
    return "rejected"


async def _judge_batch(
    client: AIClient,
    batch: list[AdRecord],
    batch_no: int,
    intent: str,
    additional_filters: str | None = None,
    profile_text: str | None = None,
    request_id: str | None = None,
) -> dict[str, AdRelevanceVerdict]:
    """
    One Gemini call for up to BATCH_SIZE ads. Never raises: any failure defers
    the whole batch (fail-closed) with an explanatory reason.

    Fail-closed means "deferred", not "rejected": a checkpoint failure must
    never throw away a lead the user might want. Deferred ads stay in the
    profile and are reconsidered later.
    """
    ids = [ad.ad_archive_id for ad in batch]
    listing = "\n\n".join(_format_ad(ad, i) for i, ad in enumerate(batch, start=1))

    sections = []
    if profile_text:
        sections.append(f"ABOUT YOU (defines the profile):\n{profile_text}")
    sections.append(f"CATEGORY / INTENT:\n{intent}")
    user_prompt = "\n\n".join(sections) + f"\n\n{len(batch)} ads to judge:\n\n{listing}"
    if additional_filters:
        user_prompt += (
            "\n\nADDITIONAL REQUEST (what they want RIGHT NOW — being off-target "
            f"here means DEFERRED, not rejected):\n{additional_filters}"
        )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    def fail_closed(reason: str) -> dict[str, AdRelevanceVerdict]:
        return {
            ad_id: AdRelevanceVerdict(
                ad_archive_id=ad_id, outcome="deferred", confidence=0.0,
                reason=reason, request_id=request_id, ai_failed=True,
            )
            for ad_id in ids
        }

    try:
        raw = await client.blocking(
            messages, max_tokens=2400, increment=400, temperature=0.0,
        )
    except Exception as e:
        logger.error("🚫 Relevance batch %d failed entirely — deferring: %s", batch_no, e)
        return fail_closed("checkpoint error — will be retried")

    verdicts: dict[str, AdRelevanceVerdict] = {}
    rows = raw.get("verdicts") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        logger.error("🚫 Relevance batch %d returned malformed JSON — deferring", batch_no)
        return fail_closed("malformed model output — will be retried")

    known = set(ids)
    has_request = bool(additional_filters)
    for row in rows:
        if not isinstance(row, dict):
            continue
        ad_id = str(row.get("id", "")).strip()
        if ad_id not in known or ad_id in verdicts:
            continue  # hallucinated or duplicate id — ignore
        row["_has_request"] = has_request
        outcome = _parse_outcome(row)
        verdicts[ad_id] = AdRelevanceVerdict(
            ad_archive_id=ad_id,
            outcome=outcome,
            # Only a rejection carries a kind; the schema default covers the rest.
            rejection_kind=(
                _parse_rejection_kind(row) if outcome == "rejected" else "not_for_me"
            ),
            confidence=_normalize_confidence(row.get("confidence")),
            reason=str(row.get("reason", "")).strip(),
            ad_category=str(row.get("category", "")).strip(),
            request_id=request_id,
        )

    # Ads the model stayed silent about → deferred AND treated as a failure:
    # the model was instructed to answer for EVERY id, so silence is a protocol
    # breach, not a judgement.
    for ad_id in ids:
        if ad_id not in verdicts:
            verdicts[ad_id] = AdRelevanceVerdict(
                ad_archive_id=ad_id, outcome="deferred", confidence=0.0,
                reason="no verdict returned — will be retried",
                request_id=request_id, ai_failed=True,
            )

    tally = {o: sum(1 for v in verdicts.values() if v.outcome == o)
             for o in ("accepted", "deferred", "rejected")}
    junk = sum(1 for v in verdicts.values() if v.rejection_kind == "no_hiring")
    logger.info(
        "⚖️ Relevance batch %d: %d accepted / %d deferred / %d rejected "
        "(%d offer no employment at all)",
        batch_no, tally["accepted"], tally["deferred"], tally["rejected"], junk,
    )
    return verdicts


async def judge_ad_relevance(
    ads: list[AdRecord],
    intent: str | None = None,
    batch_size: int = BATCH_SIZE,
    max_concurrent: int = MAX_CONCURRENT_BATCHES,
    additional_filters: str | None = None,
    profile_text: str | None = None,
    request_id: str | None = None,
) -> dict[str, AdRelevanceVerdict]:
    """
    Checkpoint (a): judge every ad in two stages — against the profile (about
    you), then against the current request. An explicit `intent` overrides the
    category description, for experiments.

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
            return await _judge_batch(
                client, batch, batch_no, description, additional_filters,
                profile_text, request_id,
            )

    results = await asyncio.gather(
        *(
            _run(batch_no, description, batch)
            for batch_no, (description, batch) in enumerate(batches, start=1)
        )
    )

    verdicts: dict[str, AdRelevanceVerdict] = {}
    for batch_result in results:
        verdicts.update(batch_result)

    tally = {o: sum(1 for v in verdicts.values() if v.outcome == o)
             for o in ("accepted", "deferred", "rejected")}
    shaky = sum(1 for v in verdicts.values()
                if v.outcome == "accepted" and v.confidence < 0.6)
    failed = sum(1 for v in verdicts.values() if v.ai_failed)
    junk = sum(1 for v in verdicts.values()
               if v.outcome == "rejected" and v.rejection_kind == "no_hiring")
    logger.info(
        "✅ Checkpoint (a): %d accepted / %d deferred / %d rejected of %d "
        "(%d no-employer)\u0020%s%s",
        tally["accepted"], tally["deferred"], tally["rejected"], len(verdicts), junk,
        f" — {shaky} accepted below 0.6 confidence" if shaky else "",
        f" — {failed} lost to AI failure (will retry next run)" if failed else "",
    )
    return verdicts
