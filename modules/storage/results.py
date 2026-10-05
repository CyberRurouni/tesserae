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

from modules.families.index import get_family_index
from modules.families import store as family_store
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
    family_id: str | None = None,
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
    #
    # Recorded against the FAMILY, not against a hash of the profile text. That
    # hash was the original duplicate bug: one edited word rotated the entire
    # key space and every previously judged ad looked unseen again. A family
    # records LINEAGE, so a new node inherits its ancestors' verdicts instead.
    newly_marked = 0
    family_index = get_family_index(category_id, family_id) if family_id else None
    if family_index is None:
        logger.warning("⚠️ No family for these results — verdicts not recorded (dedup stays within-run only)")
    for ad in ads:
        v = verdicts[ad.ad_archive_id]
        if v.ai_failed or family_index is None:
            continue
        if not family_index.has(ad.ad_archive_id):
            newly_marked += 1
        family_index.record(
            ad.ad_archive_id,
            "accepted" if v.relevant else "rejected",
            reason=v.reason,
            ad_category=ad.category or "",
        )

    cat_dir = ADS_DIR / category_id
    cat_dir.mkdir(parents=True, exist_ok=True)

    # Accepted ads go to the family's LEAD LIST: deduped, never pruned, and
    # mirrored to JSON so a row can never be lost to a Redis flush. This
    # replaces the old append-only store, which had grown to 47 rows for 26
    # unique ads because nothing ever checked.
    if family_id and accepted:
        added = family_store.add_leads(
            family_id, [ad.model_dump(mode="json") for ad in accepted]
        )
        if added:
            logger.info("📥 Lead list for %s: %d new lead(s)", family_id, added)

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
