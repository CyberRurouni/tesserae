"""
results.py — turn checkpoint verdicts into durable facts.

One function, used by every driver (smoke runner, orchestrator):
  1. Splits ads into accepted / judged-rejects / AI-failures.
  2. Marks FINAL verdicts in Redis db1 under the category namespace
     (AI-failures stay unmarked -> free retry next run).
  3. Appends accepted ads to the category's permanent store and writes the
     run's diagnostic file (one per run_id, merged across cycles).
"""

import json
import logging
from datetime import datetime, timezone

from core import ADS_DIR, AdRelevanceVerdict, AdRecord

from modules.profiles import deferred as deferred_store
from modules.profiles import held as held_store
from modules.profiles import store as profile_store
from modules.storage.run_results import append_results, begin_run
from modules.verdicts import (
    FilterIndex as get_filter_index,
    record_accepted,
    record_junk,
    record_rejected,
)
from core import ScrapingFilters

logger = logging.getLogger(__name__)


def _merge_by_ad_id(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """Existing entries first, then fresh ones not already listed (by ad_archive_id)."""
    merged = [e for e in existing if isinstance(e, dict)]
    seen = {e.get("ad_archive_id") for e in merged}
    for entry in fresh:
        ad_id = entry.get("ad_archive_id")
        if ad_id in seen:
            continue
        seen.add(ad_id)
        merged.append(entry)
    return merged


def record_results(
    ads: list[AdRecord],
    verdicts: dict[str, AdRelevanceVerdict],
    category_id: str,
    filters: ScrapingFilters | None = None,
    run_id: str | None = None,
    searched_keywords: list[str] | None = None,
    profile_id: str | None = None,
    request_id: str | None = None,
    profile_threshold: float = 0.5,
    request_threshold: float = 0.5,
) -> dict:
    """
    Apply verdicts: mark + store + write the run files under
    data/ads/<category>/. Returns a summary dict for logging/printing.

    run_id: name of the ONE file this run writes (<run_id>.json, appending to
    an existing file so multi-cycle runs accumulate). None (CLI/legacy
    callers) keeps a fresh timestamped run_<stamp>.json per call.
    searched_keywords: every keyword searched so far in this run — merged into
    the file's top-level "keywords" list the UI displays.
    """

    accepted: list[AdRecord] = []
    accepted_entries: list[dict] = []
    held: list[dict] = []
    deferred: list[dict] = []
    judged_rejects: list[dict] = []
    failed_ads: list[dict] = []

    for ad in ads:
        v = verdicts[ad.ad_archive_id]
        entry = {
            **ad.model_dump(mode="json"),
            "relevance_reason": v.reason,
            "relevance_confidence": v.confidence,
            "ad_category": v.ad_category,
        }
        if v.stage:
            entry["relevance_stage"] = v.stage
        if v.outcome == "accepted":
            accepted.append(ad)
            accepted_entries.append(entry)
        elif v.outcome == "held":
            entry["held_threshold"] = v.held_threshold
            entry["held_kind"] = v.held_kind
            held.append(entry)
        elif v.ai_failed:
            # A checkpoint failure is not a judgement. It goes to failed_ads so
            # it is never recorded as final, and it stays eligible next run.
            failed_ads.append(entry)
        elif v.outcome == "deferred":
            deferred.append(entry)
        else:
            judged_rejects.append(entry)

    # Final verdicts only — AI-failures stay unmarked, so they are retried.
    #
    # Each outcome goes to the set whose reach matches how certain the answer
    # is. "no_hiring" is a statement about the ad itself and is remembered
    # globally; "not_for_me" is only true against one profile. Putting a
    # not_for_me rejection in the global junk set would discard a lead another
    # profile would have valued, so the two are kept strictly apart.
    newly_marked = 0
    filter_index = get_filter_index(
        category_id, profile_id, request_id, profile_threshold, request_threshold,
    )
    accepted_ids, junk_ids, rejected_ids = [], [], []

    for ad in ads:
        v = verdicts[ad.ad_archive_id]
        if v.ai_failed:
            continue
        ad_id = ad.ad_archive_id
        if not filter_index.check(ad_id)[0]:
            newly_marked += 1

        if v.outcome == "accepted":
            accepted_ids.append(ad_id)
            # A lead never sits in a waiting bucket.
            deferred_store.resolve(profile_id, ad_id)
            held_store.resolve(profile_id, ad_id)
        elif v.outcome == "held":
            # Not marked anywhere: the ad is waiting on a threshold change, not
            # on another scrape. It is written to the held store below.
            pass
        elif v.outcome == "deferred":
            held_store.resolve(profile_id, ad_id)
        elif v.rejection_kind == "no_hiring":
            junk_ids.append(ad_id)
            held_store.resolve(profile_id, ad_id)
        else:
            rejected_ids.append(ad_id)
            held_store.resolve(profile_id, ad_id)

    if accepted_ids:
        record_accepted(category_id, accepted_ids)
    if junk_ids:
        record_junk(category_id, junk_ids)
    if rejected_ids and profile_id:
        record_rejected(profile_id, rejected_ids)
    filter_index.refresh()

    cat_dir = ADS_DIR / category_id
    cat_dir.mkdir(parents=True, exist_ok=True)

    # Accepted ads go to the profile's LEAD LIST: deduped, never pruned, and
    # mirrored to JSON so a row can never be lost to a Redis flush. This
    # replaces the old append-only store, which had grown to 47 rows for 26
    # unique ads because nothing ever checked.
    if profile_id and accepted_entries:
        added = profile_store.add_leads(profile_id, accepted_entries)
        if added:
            logger.info("📥 Lead list for %s: %d new lead(s)", profile_id, added)

    # Deferred ads keep their FULL record so a later request profile can
    # re-judge them without re-scraping.
    if profile_id:
        for entry in deferred:
            deferred_store.defer_ad(
                profile_id, entry, request_id or "",
                reason=entry.get("relevance_reason", ""),
                ad_category=entry.get("ad_category", ""),
            )
        if deferred:
            logger.info("⏸️  %d ad(s) deferred for %s", len(deferred), profile_id)

        # Held ads already passed the profile test. Their confidence and the
        # threshold they missed are kept so a lower slider can promote them
        # without scraping or re-judging the profile.
        for entry in held:
            held_store.hold_ad(
                profile_id, entry,
                stage=entry.get("relevance_stage") or "profile",
                request_id=request_id,
                confidence=float(entry.get("relevance_confidence") or 0.0),
                threshold=float(entry.get("held_threshold") or 0.5),
                reason=entry.get("relevance_reason", ""),
                ad_category=entry.get("ad_category", ""),
            )
        if held:
            logger.info("🤔 %d ad(s) held below the threshold for %s", len(held), profile_id)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_path = cat_dir / (f"{run_id}.json" if run_id else f"run_{stamp}.json")

    buckets = {
        "accepted": accepted_entries,
        "held": held,
        "deferred": deferred,
        "judged_rejected": judged_rejects,
        "ai_failed": failed_ads,
    }

    if run_id:
        # Staged in Redis, written once when the run ends.
        #
        # This used to rewrite the whole file after every judged group. JSON
        # cannot be appended to, so each save re-serialised everything already
        # stored: a keyword returning 1,400 ads wrote 95 MB to end up with 2 MB,
        # and streaming the harvest turned one save into seventy. Redis RPUSH is
        # the same cost at any size, and because Redis is a separate process the
        # data outlives the app.
        append_results(run_id, buckets, searched_keywords)
    else:
        # No run lifecycle to hang a final write off (CLI and legacy callers), so
        # this still writes immediately and merges into its own earlier file.
        _write_run_file_now(
            run_path, buckets, searched_keywords, stamp,
            {"profile": profile_threshold, "request": request_threshold},
        )

    logger.info(
        "🗄️ Verdicts recorded: %d accepted, %d held, %d deferred, "
        "%d judged-rejects, %d AI-failures (%d newly marked)",
        len(accepted), len(held), len(deferred), len(judged_rejects),
        len(failed_ads), newly_marked,
    )
    return {
        "accepted": accepted,
        "accepted_entries": accepted_entries,
        "held": held,
        "judged_rejects": judged_rejects,
        "failed_ads": failed_ads,
        "deferred": deferred,
        "newly_marked": newly_marked,
    }


def _write_run_file_now(
    run_path, buckets: dict, searched_keywords, stamp: str,
    thresholds: dict[str, float] | None = None,
) -> None:
    """Merge-and-write, for callers with no run lifecycle to defer to."""
    payload = {name: list(items or []) for name, items in buckets.items()}
    payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
    payload["thresholds"] = thresholds or {"profile": 0.5, "request": 0.5}

    previous: dict = {}
    if run_path.exists():
        try:
            previous = json.loads(run_path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            logger.warning("⚠️ Unreadable run file %s — starting a fresh one", run_path)
            previous = {}

    if previous:
        for name in ("accepted", "held"):
            payload[name] = _merge_by_ad_id(previous.get(name) or [],
                                            payload[name])
        # `previous.get(...) or []` keeps run files written before the deferred
        # bucket existed readable — they simply have nothing in it.
        for name in ("deferred", "judged_rejected", "ai_failed"):
            payload[name] = (previous.get(name) or []) + payload[name]
        # recorded_at stays the run's start, not this cycle's write time
        payload["recorded_at"] = previous.get("recorded_at") or payload["recorded_at"]
        payload["thresholds"] = previous.get("thresholds") or payload["thresholds"]

    # Keyword union (order kept, no dupes) — the UI shows this list, so it must
    # include keywords that returned zero ads too.
    keywords: list[str] = []
    for kw in list(previous.get("keywords") or []) + list(searched_keywords or []):
        if isinstance(kw, str) and kw and kw not in keywords:
            keywords.append(kw)
    payload["keywords"] = keywords

    run_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
