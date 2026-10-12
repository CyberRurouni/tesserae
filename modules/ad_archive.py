"""
ad_archive.py — find the record behind a verdict.

The four sets store only ad ids, which is what makes a membership check a
single O(1) lookup on the hot path. The viewer needs the other half: given an
id, what was the ad actually say? That data already exists — every run file
kept the full records it judged — so this module builds a reverse index over
those files rather than starting a new store.

Nothing here is authoritative. A missing record is normal (an id can be in a
set from a run file that has since been rotated out), so callers must render a
row from the id alone rather than treating absence as an error.

The index is cached per process and invalidated by the newest mtime among the
scanned files, so a finished run shows up without a restart.
"""

import json
import logging
from pathlib import Path

from genesis.config import ADS_DIR

logger = logging.getLogger(__name__)

CATEGORY = "career_jobs"

# Run files nest these; the shapes differ slightly but all carry ad_archive_id.
_RECORD_SECTIONS = ("accepted", "judged_rejected", "deferred", "ai_failed", "ads")

_index: dict[str, dict] = {}
_signature: tuple | None = None


def _category_dir(category: str) -> Path:
    return ADS_DIR / category


def _scan(category: str) -> tuple[tuple, dict[str, dict]]:
    """
    Read every stored record for a category.

    Returns (signature, index) where the signature is the sorted mtime/size
    pair of the files that were read — the cheapest thing that changes when a
    run lands, and enough to know the cache is stale.
    """
    index: dict[str, dict] = {}
    signature: list[tuple] = []
    root = _category_dir(category)
    if not root.exists():
        return (), index

    for path in sorted(root.glob("*.json")):
        try:
            stat = path.stat()
            signature.append((path.name, stat.st_mtime_ns, stat.st_size))
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("⚠️ Skipping unreadable archive %s (%s)", path.name, exc)
            continue

        records = []
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            for section in _RECORD_SECTIONS:
                value = data.get(section)
                if isinstance(value, list):
                    records.extend(value)
        for record in records:
            if not isinstance(record, dict):
                continue
            ad_id = record.get("ad_archive_id")
            if not ad_id:
                continue
            # First writer wins, so the earliest run that judged an ad is the
            # record shown rather than the latest re-encoding of it.
            index.setdefault(str(ad_id), record)
    return tuple(sorted(signature)), index


def _records(category: str) -> dict[str, dict]:
    global _index, _signature
    signature, fresh = _scan(category)
    if _signature != signature or not fresh:
        _index, _signature = fresh, signature
        logger.info("📚 Ad archive for %s: %d record(s)", category, len(_index))
    return _index


def get_ad(category: str, ad_id: str) -> dict | None:
    """The stored record for one ad, or None if it is no longer on disk."""
    return _records(category).get(str(ad_id))


def summarize(record: dict | None, ad_id: str, outcome: str) -> dict:
    """
    One row for the viewer.

    Built so a missing record still renders: the id and the verdict are always
    known even when the original ad text is gone.
    """
    record = record or {}
    text = (record.get("ad_text") or "").strip()
    return {
        "ad_archive_id": ad_id,
        "outcome": outcome,
        "advertiser_name": record.get("advertiser_name") or "(name unavailable)",
        "advertiser_page_url": record.get("advertiser_page_url") or "",
        "platforms": record.get("platforms") or [],
        "source_keyword": record.get("source_keyword") or "",
        "ad_text": text,
        "excerpt": (text[:280] + "…") if len(text) > 280 else text,
        "cta_text": record.get("cta_text") or "",
        "cta_url": record.get("cta_url") or "",
        "started_date": record.get("started_date") or "",
        "relevance_reason": record.get("relevance_reason") or "",
        "relevance_confidence": record.get("relevance_confidence"),
        "heuristic_labels": record.get("heuristic_labels") or [],
        "has_record": bool(record),
    }


def invalidate() -> None:
    """Drop the cache. Used by tests that write archive files directly."""
    global _index, _signature
    _index, _signature = {}, None
