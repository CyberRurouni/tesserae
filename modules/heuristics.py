"""
heuristics.py — the user's own first-sieve patterns.

The category ships a built-in sieve ("hiring" required, "course"/"enroll"
excluded). That is a starting point, not a ceiling: a user who keeps seeing a
bootcamp ad slip through needs to be able to add "bootcamp" themselves, and
one whose employer phrases things unusually needs to relax a requirement.

Two rules make this safe to edit:

  1. User patterns ADD to the category's, they never replace it. Losing
     "hiring" because you edited the exclude list would quietly widen every
     run, and the symptom would only show up weeks later as junk leads.
  2. Nothing here is ever a verdict. A pattern can only keep an ad from
     reaching the AI, and an over-broad pattern is invisible — which is why
     every rule the user sets is listed back to them with a live count of
     what it would drop.
"""

import json
import logging
from pathlib import Path

from genesis.config import DATA_DIR

logger = logging.getLogger(__name__)

HEURISTICS_FILE = DATA_DIR / "heuristics.json"

FIELDS = (
    "require_ad_text_patterns",
    "require_any_ad_text_patterns",
    "exclude_ad_text_patterns",
)

LABELS = {
    "require_ad_text_patterns": "Must contain all of",
    "require_any_ad_text_patterns": "Must contain at least one of",
    "exclude_ad_text_patterns": "Must never contain",
}


def _blank() -> dict[str, list[str]]:
    return {field: [] for field in FIELDS}


def load() -> dict[str, list[str]]:
    """The user's patterns, normalised. A corrupt file degrades to empty."""
    if not HEURISTICS_FILE.exists():
        return _blank()
    try:
        raw = json.loads(HEURISTICS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("❌ heuristics.json unreadable (%s) — starting empty", exc)
        return _blank()
    out = _blank()
    if isinstance(raw, dict):
        for field in FIELDS:
            value = raw.get(field)
            if isinstance(value, list):
                out[field] = [str(v).strip() for v in value if str(v).strip()]
    return out


def save(patterns: dict[str, list[str]]) -> dict[str, list[str]]:
    """Write the whole set, dropping blanks and duplicates. Returns what stuck."""
    cleaned = {
        field: sorted({str(v).strip() for v in patterns.get(field, []) if str(v).strip()})
        for field in FIELDS
    }
    HEURISTICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEURISTICS_FILE.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")
    logger.info(
        "🧩 Heuristics saved: %s",
        ", ".join(f"{LABELS[f]}={len(cleaned[f])}" for f in FIELDS if cleaned[f]) or "(none)",
    )
    return cleaned


def merged(category_heuristics: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """
    Category patterns plus the user's, deduplicated.

    Order is irrelevant to the scraper (it tests every pattern in a list) but
    stable here so the UI can show the same list twice and have it match.
    """
    base = {field: list((category_heuristics or {}).get(field, [])) for field in FIELDS}
    mine = load()
    for field in FIELDS:
        base[field] = sorted({str(v).strip() for v in base[field] + mine[field] if str(v).strip()})
    return base


def preview(patterns: dict[str, list[str]], texts: list[str]) -> dict[str, dict[str, int]]:
    """
    What each pattern would drop, counted against real ad text.

    An over-broad rule is the failure mode here: "hire" would exclude every ad
    that says "we're hiring", including the leads you wanted. Showing the count
    per pattern is the cheapest way to make that visible before a run, rather
    than after.
    """
    lowered = [(t or "").lower() for t in texts]
    report: dict[str, dict[str, int]] = {}
    for field in FIELDS:
        report[field] = {}
        for pattern in patterns.get(field, []):
            needle = pattern.lower()
            hits = sum(1 for text in lowered if needle in text)
            report[field][pattern] = hits
    return report


def combined_for_run(category_heuristics: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """
    What the scraper should use for one run.

    Only the exclude and OR-require lists can grow safely from the UI. The
    AND-require list is deliberately excluded: every pattern added there
    narrows the sieve further, and stacking a few of them can make it drop
    everything, which looks like "no results" rather than a bad filter.
    """
    both = merged(category_heuristics)
    return {
        "require_ad_text_patterns": [],   # category's own only — see docstring
        "require_any_ad_text_patterns": both["require_any_ad_text_patterns"],
        "exclude_ad_text_patterns": both["exclude_ad_text_patterns"],
    }


def path() -> Path:
    return HEURISTICS_FILE
