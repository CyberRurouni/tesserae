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

from core import ADS_DIR, AdRelevanceVerdict, AdRecord, PROFILE_HASH_TTL

from modules.scraper.seen import get_seen_index
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
    judged_rejects: list[dict] = []
    failed_ads: list[dict] = []

    for ad in ads:
        v = verdicts[ad.ad_archive_id]
        entry = {
            **ad.model_dump(mode="json"),
            "relevance_reason": v.reason,
            "relevance_confidence": v.confidence,
        }
        if v.relevant:
            accepted.append(ad)
        elif v.ai_failed:
            failed_ads.append(entry)
        else:
            judged_rejects.append(entry)

    # Final verdicts only — AI-failures stay unmarked (free retry later).
    # Goes through the shared in-memory mirror so an ad judged in cycle 1 is
    # skipped by cycle 2 of the SAME run without a Redis round trip per card.
    seen_index = get_seen_index(category_id, filters)
    newly_marked = 0
    for ad in ads:
        v = verdicts[ad.ad_archive_id]
        if not v.ai_failed and seen_index.mark(ad.ad_archive_id, ttl=PROFILE_HASH_TTL):
            newly_marked += 1

    cat_dir = ADS_DIR / category_id
    cat_dir.mkdir(parents=True, exist_ok=True)

    store_path = cat_dir / "stored_ads.json"
    store = json.loads(store_path.read_text()) if store_path.exists() else []
    store.extend(ad.model_dump(mode="json") for ad in accepted)
    store_path.write_text(json.dumps(store, indent=2, ensure_ascii=False))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_path = cat_dir / (f"{run_id}.json" if run_id else f"run_{stamp}.json")

    accepted_entries = [ad.model_dump(mode="json") for ad in accepted]
    payload = {
        "accepted": accepted_entries,
        "judged_rejected": judged_rejects,
        "ai_failed": failed_ads,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

    # One file per user-initiated run: merge into whatever this run wrote earlier.
    previous: dict = {}
    if run_path.exists():
        try:
            previous = json.loads(run_path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            logger.warning("⚠️ Unreadable run file %s — starting a fresh one", run_path)
            previous = {}

    if previous:
        payload["accepted"] = _merge_by_ad_id(previous.get("accepted") or [], accepted_entries)
        payload["judged_rejected"] = (previous.get("judged_rejected") or []) + judged_rejects
        payload["ai_failed"] = (previous.get("ai_failed") or []) + failed_ads
        # recorded_at stays the run's start, not this cycle's write time
        payload["recorded_at"] = previous.get("recorded_at") or payload["recorded_at"]

    # Keyword union (order kept, no dupes) — the UI shows this list, so it must
    # include keywords that returned zero ads too.
    keywords: list[str] = []
    for kw in list(previous.get("keywords") or []) + list(searched_keywords or []):
        if isinstance(kw, str) and kw and kw not in keywords:
            keywords.append(kw)
    payload["keywords"] = keywords

    run_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    logger.info(
        "🗄️ Verdicts recorded: %d accepted, %d judged-rejects, %d AI-failures (%d newly marked)",
        len(accepted), len(judged_rejects), len(failed_ads), newly_marked,
    )
    return {
        "accepted": accepted,
        "judged_rejects": judged_rejects,
        "failed_ads": failed_ads,
        "newly_marked": newly_marked,
    }
