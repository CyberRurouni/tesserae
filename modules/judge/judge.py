"""
judge.py — relevance judge: is this ad actually what the user wants?

Two judges, deliberately separate:

  A. PROFILE  — who the person IS. No request text at all. Decides whether the
     ad offers employment and whether the work is theirs. Outcomes are only
     accepted or rejected (with a rejection kind).

  B. REQUEST  — what they want RIGHT NOW. Sees only the ads A accepted. The
     request is placed BEFORE the ads because it is the lens, not an appendix.
     Outcomes are only accepted or deferred: the request can say "not now", but
     it can never declare a role outside the person's field.

Confidence is used only to gate ACCEPTED ads. An accepted ad below the selected
threshold is `held`, not rejected: it carries the threshold it missed, so
lowering the slider can promote it later without re-scraping. Rejections and
deferrals are never thresholded.

Failure handling stays fail-closed: a failed or malformed model reply defers the
batch and is marked ai_failed, so nothing is accepted on a guess and nothing is
permanently discarded by an outage.
"""

import asyncio
import logging
from typing import Awaitable, Callable, Optional

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
DEFAULT_THRESHOLD = 0.5
MIN_THRESHOLD = 0.5
MAX_THRESHOLD = 0.9

_PROFILE_SYSTEM_PROMPT = """\
You are a strict PROFILE-relevance judge in a lead-generation pipeline.

You receive ABOUT YOU and the CATEGORY being searched, then a numbered list of
ads. There is deliberately NO request text: what the person wants right now is
judged by a separate stage and must not influence this answer.

Decide in TWO stages, in this order, for every ad. Stop at the first stage that
answers.

STAGE 1 — does this ad offer actual employment to anyone?
  NO  -> outcome "rejected", rejection_kind "no_hiring".
          Nobody is being employed: a course, bootcamp, certification, webinar,
          B2B / recruiting / outsourcing / staffing service sold TO employers,
          job board, marketplace, agency product, or content such as a podcast,
          talk or newsletter. A company that sells recruitment services to
          employers is a service provider, not an employer.
  YES -> go to stage 2.

  A staffing or placement agency that advertises a specific open role for a
  client IS offering employment to candidates. It is not "no_hiring" merely
  because the employer is a client. The same applies to a walk-in drive or a
  recruiter posting a named vacancy.

STAGE 2 — is the role inside what this person actually does?
  NO  -> outcome "rejected", rejection_kind "not_for_me".
          An employer IS hiring, but not for them: a different field or a
          different stack. This is specific to THIS person — another profile
          might well want it, so it is never universally useless.
  YES -> outcome "accepted".

  Seniority is NOT a profile question. A senior or junior version of the work
  is still the same field. Do not reject because the ad asks for more or fewer
  years, a different contract type, a location, remote/on-site, salary, or start
  date. Those belong to the request judge.

  Stage 2 is about the ROLE, not the vocabulary around it. Judge what the person
  would actually DO all day, and require that the ad names that work as the job
  itself — not as a tool the job happens to touch, not as a team they would
  interface with, not as an automation they would maintain.

  Mentions of your field do not make it your field. A virtual assistant /
  account manager role that will "support website, automation and AI-related
  delivery", and asks for media-buying and design skills, is an operations and
  client-services job. It is not a software engineering role, however many of
  your keywords appear in it. The same applies to roles that merely coordinate,
  oversee, administer or sell the kind of work you do.

  Ask: if this person were hired, would their actual day-to-day be the work
  their profile describes? If the honest answer is "some of it, alongside a lot
  of something else", that is "not_for_me".

Do NOT reject for request-only matters. Location, remote vs on-site, company
country, seniority, contract type, salary and start date are NOT profile
questions. They belong to the request judge, which can defer an otherwise
relevant ad. This judge only answers: does the ad employ someone, and is the
work theirs?

Judging rules:
- Judge the ad's CONTENT, never the mere presence of keywords. An ad containing
  the word "hiring" is not enough — it must actually offer a role.
- When genuinely unsure at stage 1, answer "no_hiring".
- When genuinely unsure at stage 2, accept if the ad names the work as the job
  itself. Reject only when the role is clearly outside the field; the request
  judge can still defer an in-field ad that does not fit right now.
- Give "confidence" as your certainty in an ACCEPTED answer. For a rejection it
  is recorded but not used for gating.

Also give each ad a short "category" naming the work it is about (e.g.
"Backend development", "Data science").

Answer with JSON only, exactly this shape, one entry per input id and no extra
ids:
{"verdicts": [{"id": "<ad id>", "outcome": "accepted|rejected", \
"rejection_kind": "no_hiring|not_for_me", "category": "<max 3 words>", \
"confidence": <0.0-1.0>, "reason": "<max 12 words>"}]}
"""

_REQUEST_SYSTEM_PROMPT = """\
You are a strict REQUEST-fit judge in a lead-generation pipeline.

The ads below have ALREADY passed the profile judge. They employ people, and the
work is in this person's field. Do NOT re-judge whether the role or field fits.
Your only job is whether it is wanted under the REQUEST at the top of this
message. The request comes before the ads on purpose: it is the lens.

Return ONLY "accepted" or "deferred" for every ad. Never "rejected": a request
cannot say a role is outside the person's profession.

HARD CONSTRAINTS
- Read the request and separate MUST / MUST NOT requirements from soft
  preferences. Words such as "must", "only", "required", "not open to", "not
  interested in", "never" and "exclude" mark hard constraints.
- Apply every hard constraint literally. If the ad explicitly violates one,
  outcome "deferred".
- Silence is NOT a conflict. If the ad does not state a hard constraint, do not
  defer for it. Only defer when the ad's own text explicitly contradicts a hard
  constraint.
- A remote or global ad that names no candidate-country restriction is open on
  location. Do not infer a country restriction from the word "remote", from the
  company's headquarters, or from the markets the company serves.
- A company-location constraint is about where the company is based, not the
  nationality of teammates. An on-site exception is about the job's location.
- Seniority is a hard constraint only when the request states a limit. If the
  ad's requirement is within or below the request's limit, accept it. Defer
  only when the ad explicitly requires more than the request allows.
- Do not infer a requirement from a word the request does not say. "Remote"
  does not mean "worldwide"; "must not be based in India" does not reject an ad
  that simply mentions India as a market.

EXAMPLES
- Request: "Only Pakistan and in Pakistan only Islamabad."
  Defer: "We are hiring in Mumbai"; "Hiring only Europeans"; "Now hiring
  Asians, except for Pakistanis."
  Accept: "We are hiring"; "Hiring worldwide"; "Looking for candidates in
  Europe and Asia, including India and Bangladesh"; "Remote, no location
  restriction stated."
- A company based abroad is not a location conflict unless the ad says the
  candidate must be there or must not be in Pakistan.

SOFT PREFERENCES
- "Prefer", "ideally", "open to" and similar are preferences. They affect
  confidence and the reason, never the outcome on their own.
- If all hard constraints are satisfied but preferences do not match, still
  accept when the ad is clearly wanted under the request.

CONFIDENCE
- Confidence is your certainty that the ad satisfies the request. A crisp,
  explicit match is high. A match that requires a reasonable inference from the
  ad's own text is lower.
- The caller may hold an accepted ad whose confidence is below the user's
  strictness threshold. That is not a rejection.

Answer with JSON only, exactly this shape, one entry per input id and no extra
ids:
{"verdicts": [{"id": "<ad id>", "outcome": "accepted|deferred", \
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


def _normalize_threshold(raw, default: float = DEFAULT_THRESHOLD) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    if value > 1.0:
        value /= 100.0
    return min(max(value, MIN_THRESHOLD), MAX_THRESHOLD)


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


def _parse_profile_outcome(row: dict) -> str:
    """Profile judge: only accepted or rejected. Anything else is a rejection."""
    value = str(row.get("outcome") or "").strip().lower()
    if value in ("accepted", "rejected"):
        return value
    if "relevant" in row:
        return "accepted" if row.get("relevant") else "rejected"
    return "rejected"


def _parse_request_outcome(row: dict) -> str:
    """Request judge: only accepted or deferred. A rejection becomes deferred."""
    value = str(row.get("outcome") or "").strip().lower()
    if value == "accepted":
        return "accepted"
    if "relevant" in row:
        return "accepted" if row.get("relevant") else "deferred"
    return "deferred"


def _held_kind(confidence: float) -> str:
    return "very_low" if confidence < MIN_THRESHOLD else "borderline"


def _apply_threshold(
    verdict: AdRelevanceVerdict, threshold: float, stage: str,
) -> AdRelevanceVerdict:
    """Turn an accepted verdict below the threshold into a held verdict."""
    if verdict.outcome != "accepted" or verdict.confidence >= threshold:
        if verdict.outcome == "accepted":
            verdict.stage = stage
        return verdict
    return verdict.model_copy(update={
        "outcome": "held",
        "stage": stage,
        "held_threshold": threshold,
        "held_kind": _held_kind(verdict.confidence),
    })


async def _judge_profile_batch(
    client: AIClient,
    batch: list[AdRecord],
    batch_no: int,
    intent: str,
    profile_text: str | None = None,
    request_id: str | None = None,
) -> dict[str, AdRelevanceVerdict]:
    """One Gemini call for the profile stage. Never raises."""
    ids = [ad.ad_archive_id for ad in batch]
    listing = "\n\n".join(_format_ad(ad, i) for i, ad in enumerate(batch, start=1))

    sections = []
    if profile_text:
        sections.append(f"ABOUT YOU (defines the profile):\n{profile_text}")
    sections.append(f"CATEGORY / INTENT:\n{intent}")
    user_prompt = (
        "\n\n".join(sections)
        + f"\n\n{len(batch)} ads to judge against this profile:\n\n{listing}"
    )

    messages = [
        {"role": "system", "content": _PROFILE_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    def fail_closed(reason: str) -> dict[str, AdRelevanceVerdict]:
        return {
            ad_id: AdRelevanceVerdict(
                ad_archive_id=ad_id, outcome="deferred", confidence=0.0,
                reason=reason, request_id=request_id, stage="profile", ai_failed=True,
            )
            for ad_id in ids
        }

    try:
        raw = await client.blocking(
            messages, max_tokens=2400, increment=400, temperature=0.0,
        )
    except Exception as e:
        logger.error("🚫 Profile judge batch %d failed entirely — deferring: %s", batch_no, e)
        return fail_closed("profile judge error — will be retried")

    verdicts: dict[str, AdRelevanceVerdict] = {}
    rows = raw.get("verdicts") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        logger.error("🚫 Profile judge batch %d returned malformed JSON — deferring", batch_no)
        return fail_closed("malformed profile judge output — will be retried")

    known = set(ids)
    for row in rows:
        if not isinstance(row, dict):
            continue
        ad_id = str(row.get("id", "")).strip()
        if ad_id not in known or ad_id in verdicts:
            continue  # hallucinated or duplicate id — ignore
        outcome = _parse_profile_outcome(row)
        verdicts[ad_id] = AdRelevanceVerdict(
            ad_archive_id=ad_id,
            outcome=outcome,
            rejection_kind=(
                _parse_rejection_kind(row) if outcome == "rejected" else "not_for_me"
            ),
            confidence=_normalize_confidence(row.get("confidence")),
            reason=str(row.get("reason", "")).strip(),
            ad_category=str(row.get("category", "")).strip(),
            request_id=request_id,
            stage="profile",
        )

    # Ads the model stayed silent about → deferred AND treated as a failure:
    # the model was instructed to answer for EVERY id, so silence is a protocol
    # breach, not a judgement.
    for ad_id in ids:
        if ad_id not in verdicts:
            verdicts[ad_id] = AdRelevanceVerdict(
                ad_archive_id=ad_id, outcome="deferred", confidence=0.0,
                reason="no profile verdict returned — will be retried",
                request_id=request_id, stage="profile", ai_failed=True,
            )

    tally = {o: sum(1 for v in verdicts.values() if v.outcome == o)
             for o in ("accepted", "rejected", "deferred")}
    junk = sum(1 for v in verdicts.values() if v.rejection_kind == "no_hiring")
    logger.info(
        "⚖️ Profile judge batch %d: %d accepted / %d rejected (%d offer no employment)",
        batch_no, tally["accepted"], tally["rejected"], junk,
    )
    return verdicts


async def _judge_request_batch(
    client: AIClient,
    batch: list[AdRecord],
    batch_no: int,
    request_text: str,
    request_id: str | None = None,
    request_threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, AdRelevanceVerdict]:
    """
    One Gemini call for the request stage. Never raises, and never rejects.

    The request comes before the ads in the user message: it is the lens, not a
    trailing note the model might skim.
    """
    ids = [ad.ad_archive_id for ad in batch]
    listing = "\n\n".join(_format_ad(ad, i) for i, ad in enumerate(batch, start=1))
    user_prompt = (
        f"REQUEST (what they want RIGHT NOW — this is the lens):\n{request_text}"
        f"\n\n{len(batch)} candidate ads already accepted by the profile judge:\n\n{listing}"
    )

    messages = [
        {"role": "system", "content": _REQUEST_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    def fail_closed(reason: str) -> dict[str, AdRelevanceVerdict]:
        return {
            ad_id: AdRelevanceVerdict(
                ad_archive_id=ad_id, outcome="deferred", confidence=0.0,
                reason=reason, request_id=request_id, stage="request", ai_failed=True,
            )
            for ad_id in ids
        }

    try:
        raw = await client.blocking(
            messages, max_tokens=2400, increment=400, temperature=0.0,
        )
    except Exception as e:
        logger.error("🚫 Request judge batch %d failed entirely — deferring: %s", batch_no, e)
        return fail_closed("request judge error — will be retried")

    verdicts: dict[str, AdRelevanceVerdict] = {}
    rows = raw.get("verdicts") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        logger.error("🚫 Request judge batch %d returned malformed JSON — deferring", batch_no)
        return fail_closed("malformed request judge output — will be retried")

    known = set(ids)
    for row in rows:
        if not isinstance(row, dict):
            continue
        ad_id = str(row.get("id", "")).strip()
        if ad_id not in known or ad_id in verdicts:
            continue
        outcome = _parse_request_outcome(row)
        verdict = AdRelevanceVerdict(
            ad_archive_id=ad_id,
            outcome=outcome,
            confidence=_normalize_confidence(row.get("confidence")),
            reason=str(row.get("reason", "")).strip(),
            ad_category=str(row.get("ad_category") or row.get("category") or "").strip(),
            request_id=request_id,
            stage="request",
        )
        verdicts[ad_id] = _apply_threshold(verdict, request_threshold, "request")

    for ad_id in ids:
        if ad_id not in verdicts:
            verdicts[ad_id] = AdRelevanceVerdict(
                ad_archive_id=ad_id, outcome="deferred", confidence=0.0,
                reason="no request verdict returned — will be retried",
                request_id=request_id, stage="request", ai_failed=True,
            )

    tally = {o: sum(1 for v in verdicts.values() if v.outcome == o)
             for o in ("accepted", "deferred", "held")}
    logger.info(
        "⚖️ Request judge batch %d: %d accepted / %d deferred / %d held below %.2f",
        batch_no, tally["accepted"], tally["deferred"], tally["held"],
        request_threshold,
    )
    return verdicts


def _make_batches(
    ads: list[AdRecord], batch_size: int,
) -> list[tuple[int, list[AdRecord]]]:
    return [
        (i, ads[i - 1:i - 1 + batch_size])
        for i in range(1, len(ads) + 1, batch_size)
    ]


async def judge_request_relevance(
    ads: list[AdRecord],
    request_text: str,
    batch_size: int = BATCH_SIZE,
    max_concurrent: int = MAX_CONCURRENT_BATCHES,
    request_id: str | None = None,
    request_threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, AdRelevanceVerdict]:
    """
    Request judge only, for ads that already passed the profile judge.

    Used by the run-start re-gate path: a profile-held ad that now clears the
    lower profile threshold needs exactly this one call, not another profile
    judgement.
    """
    if not ads:
        return {}
    if not request_text.strip():
        return {
            ad.ad_archive_id: AdRelevanceVerdict(
                ad_archive_id=ad.ad_archive_id, outcome="accepted", confidence=1.0,
                reason="no request constraints", request_id=request_id, stage="request",
            )
            for ad in ads
        }

    threshold = _normalize_threshold(request_threshold)
    unique: dict[str, AdRecord] = {ad.ad_archive_id: ad for ad in ads}
    batches = _make_batches(list(unique.values()), max(1, batch_size))
    client = AIClient()
    gate = asyncio.Semaphore(max(1, max_concurrent))

    async def _run(batch_no: int, batch: list[AdRecord]):
        async with gate:
            return await _judge_request_batch(
                client, batch, batch_no, request_text, request_id, threshold,
            )

    results = await asyncio.gather(*(
        _run(batch_no, batch) for batch_no, batch in batches
    ))
    verdicts: dict[str, AdRelevanceVerdict] = {}
    for result in results:
        verdicts.update(result)
    return verdicts


async def judge_ad_relevance(
    ads: list[AdRecord],
    intent: str | None = None,
    batch_size: int = BATCH_SIZE,
    max_concurrent: int = MAX_CONCURRENT_BATCHES,
    additional_filters: str | None = None,
    profile_text: str | None = None,
    request_id: str | None = None,
    profile_threshold: float = DEFAULT_THRESHOLD,
    request_threshold: float = DEFAULT_THRESHOLD,
    on_batch: Optional[Callable[[int], Awaitable[None]]] = None,
) -> dict[str, AdRelevanceVerdict]:
    """
    Checkpoint (a): profile judge, then request judge.

    `additional_filters` keeps its historical name and is the request text. If
    it is empty there is no request lens, so profile-accepted ads are accepted
    directly and the request judge is not called.

    Returns a verdict for EVERY input ad id. Model failures are deferred with
    ai_failed=True; accepted ads below a threshold are `held`, with the
    threshold and confidence recorded.

    `on_batch` is awaited after each profile batch lands, with that batch's
    size. It exists because judging is the one long phase that reports nothing.
    """
    if not ads:
        return {}

    profile_threshold = _normalize_threshold(profile_threshold)
    request_threshold = _normalize_threshold(request_threshold)
    request_text = (additional_filters or "").strip()

    # Defensive dedup — upstream already dedups, but verdicts must be unique.
    unique: dict[str, AdRecord] = {ad.ad_archive_id: ad for ad in ads}

    # Group by category: the profile judge reads each lens's own description.
    fallback_desc = CATEGORIES[DEFAULT_CATEGORY_ID].description
    by_category: dict[str, list[AdRecord]] = {}
    for ad in unique.values():
        by_category.setdefault(ad.category or DEFAULT_CATEGORY_ID, []).append(ad)

    profile_batches: list[tuple[int, str, list[AdRecord]]] = []
    batch_no = 0
    for cat_id, group in by_category.items():
        description = intent or (
            CATEGORIES[cat_id].description if cat_id in CATEGORIES else fallback_desc
        )
        for i in range(0, len(group), batch_size):
            batch_no += 1
            profile_batches.append((batch_no, description, group[i:i + batch_size]))

    logger.info(
        "🎯 Profile judge: %d ad(s) across %d category lens(es), %d batch(es) "
        "of ≤%d (≤%d calls in flight)",
        len(unique), len(by_category), len(profile_batches), batch_size, max_concurrent,
    )

    client = AIClient()
    profile_gate = asyncio.Semaphore(max(1, max_concurrent))

    async def _run_profile(batch_no: int, description: str, batch: list[AdRecord]):
        async with profile_gate:
            try:
                return await _judge_profile_batch(
                    client, batch, batch_no, description, profile_text, request_id,
                )
            finally:
                # A batch that raises still counts as motion. A run that dies
                # mid-judging is exactly the case the tick is for.
                if on_batch is not None:
                    try:
                        await on_batch(len(batch))
                    except Exception:  # noqa: BLE001 - a tick must never break judging
                        logger.debug("⚠️ judging progress tick failed", exc_info=True)

    profile_results = await asyncio.gather(*(
        _run_profile(batch_no, description, batch)
        for batch_no, description, batch in profile_batches
    ))

    verdicts: dict[str, AdRelevanceVerdict] = {}
    for result in profile_results:
        verdicts.update(result)

    # Gate the profile accepts, then hand only those to the request judge.
    request_candidates: list[AdRecord] = []
    for ad in unique.values():
        verdict = verdicts[ad.ad_archive_id]
        if verdict.outcome != "accepted":
            continue
        gated = _apply_threshold(verdict, profile_threshold, "profile")
        verdicts[ad.ad_archive_id] = gated
        if gated.outcome == "accepted":
            request_candidates.append(ad)

    if request_text and request_candidates:
        request_batches = _make_batches(request_candidates, max(1, batch_size))
        logger.info(
            "🎯 Request judge: %d profile-accepted ad(s), %d batch(es), threshold %.2f",
            len(request_candidates), len(request_batches), request_threshold,
        )
        request_gate = asyncio.Semaphore(max(1, max_concurrent))

        async def _run_request(batch_no: int, batch: list[AdRecord]):
            async with request_gate:
                return await _judge_request_batch(
                    client, batch, batch_no, request_text, request_id,
                    request_threshold,
                )

        request_results = await asyncio.gather(*(
            _run_request(batch_no, batch)
            for batch_no, batch in request_batches
        ))
        for result in request_results:
            for ad_id, verdict in result.items():
                # The request prompt deliberately does not ask for a category
                # again. Carry the profile judge's label through so an accepted
                # lead (and a request-held ad) keeps it.
                if not verdict.ad_category:
                    previous = verdicts.get(ad_id)
                    if previous and previous.ad_category:
                        verdict = verdict.model_copy(update={
                            "ad_category": previous.ad_category,
                        })
                verdicts[ad_id] = verdict

    tally = {o: sum(1 for v in verdicts.values() if v.outcome == o)
             for o in ("accepted", "deferred", "rejected", "held")}
    shaky = sum(1 for v in verdicts.values()
                if v.outcome == "held" and v.confidence < 0.6)
    failed = sum(1 for v in verdicts.values() if v.ai_failed)
    junk = sum(1 for v in verdicts.values()
               if v.outcome == "rejected" and v.rejection_kind == "no_hiring")
    logger.info(
        "✅ Checkpoint (a): %d accepted / %d held / %d deferred / %d rejected "
        "of %d (%d no-employer)%s%s",
        tally["accepted"], tally["held"], tally["deferred"], tally["rejected"],
        len(verdicts), junk,
        f" — {shaky} held below 0.6 confidence" if shaky else "",
        f" — {failed} lost to AI failure (will retry next run)" if failed else "",
    )
    return verdicts
