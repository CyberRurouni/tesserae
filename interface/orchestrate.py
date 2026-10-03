"""
interface/orchestrate.py — Tesserae main entry point.

A single file to rule them all:
  1. Selects filters (via CLI args or interactive prompts)
  2. Saves user preferences for quick reuse next time
  3. Runs the orchestrator with the selected filters
  4. Supports --cycles and other orchestrator options

Usage:
    python -m interface.orchestrate                    # run with filter selection
    python -m interface.orchestrate --cycles 2       # cap generation rounds
    python -m interface.orchestrate --clis             # CLI-only filter mode (no prompts)
    python -m interface.orchestrate --preview          # select filters, don't run orchestrator

CLI filter selection arguments (pass any combination):
    --category     Category ID (default: last used or career_jobs)
    --delivery-status  Delivery status (default: last used or active)
    --platforms    Comma-separated platforms (default: last used or all)
    --media-type   Media type (default: last used or all)
    --date-preset  Date preset (default: last used or any)
    --keywords     Space-separated keywords (overrides generation)
    --ad-age       Ad age in hours (presets: 24, 168, 720; default: last used or none)
    --regions      Comma-separated ISO country codes (default: last used or ALL)
    --languages    Comma-separated ISO 639-1 codes (default: last used or all)
    --sort-order   Sort order (default: last used or relevance)
    --run-mode     Keyword run mode: 'existing' or 'generate' (default: generate)
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date

from core import (
    CATEGORIES,
    DEFAULT_CATEGORY_ID,
    ScrapingFilters,
    DeliveryStatus,
    Platform,
    MediaType,
    DatePreset,
    SortOrder,
    RunConfig,
)
from interface.select_filters import (
    build_scraping_filters,
    _load_last_selection,
    _save_last_selection,
)
from modules.orchestrator import run_orchestrator
from modules.storage import load_profile
from core import UserProfile

logger = logging.getLogger(__name__)

# Persistence path for filter preferences (same as select_filters)
LAST_SELECTION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".tesserae_filters.json"
)


# ── Helper: run orchestrator with given filters ────────────────────────────

async def _run_with_filters(
    filters: ScrapingFilters, cycles: int | None, run_mode: str, run_id: str | None = None
) -> dict:
    """Run the orchestrator with the given filters and return the summary.

    run_id: when given, every cycle of the run appends to ONE result file
    named <run_id>.json (the id the API handed the UI); when None (CLI) each
    record call keeps writing a fresh timestamped run_<stamp>.json.
    """
    about, _ = load_profile()
    profile = UserProfile(background=about or "(about-us file empty)")

    summary = await run_orchestrator(
        RunConfig(profile=profile, filters=filters),
        headless=False,
        max_cycles=cycles,
        run_mode=run_mode,
        run_id=run_id,
    )
    return summary


# ── CLI argument parser ────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tesserae — AI-driven Ad Prospecting Automation"
    )

    # Orchestrator options
    parser.add_argument("--cycles", type=int, default=None,
                        help="max generation rounds (default: until skills covered)")

    # Filter selection options (CLI mode — no interactive prompts)
    parser.add_argument("--category", type=str, default=None,
                        help="Category ID")
    parser.add_argument("--delivery-status", type=str, default=None,
                        choices=["all", "active", "inactive"],
                        help="Delivery status")
    parser.add_argument("--platforms", type=str, default=None,
                        help="Comma-separated platforms")
    parser.add_argument("--media-type", type=str, default=None,
                        choices=["all", "image", "video", "meme"],
                        help="Media type")
    parser.add_argument("--date-preset", type=str, default=None,
                        choices=["any", "last_7_days", "last_30_days", "custom"],
                        help="Date preset")
    parser.add_argument("--keywords", type=str, default=None,
                        help="Keywords (space-separated, or JSON array syntax): "
                             "e.g. 'python developer' or '[\"python developer\", \"backend\"]")
    parser.add_argument("--ad-age", type=int, default=None,
                        help="Ad age in hours")
    parser.add_argument("--regions", type=str, default=None,
                        help="Comma-separated ISO country codes")
    parser.add_argument("--languages", type=str, default=None,
                        help="Comma-separated ISO 639-1 codes")
    parser.add_argument("--sort-order", type=str, default=None,
                        choices=["relevance", "date"],
                        help="Sort order")
    parser.add_argument("--run-mode", type=str, default=None,
                        choices=["existing", "generate"],
                        help="Keyword run mode: 'existing' uses provided keywords, "
                             "'generate' generates then runs them")
    parser.add_argument("--preview", action="store_true",
                        help="Select filters but don't run orchestrator")

    return parser


# ── Main ───────────────────────────────────────────────────────────────────

def main(argv: list | None = None) -> None:
    """Main entry point for Tesserae.

    Handles:
    - Filter selection (CLI args or interactive prompts)
    - Preference saving/loading for quick reuse
    - Orchestrator execution with selected filters

    The flow:
    1. Parse CLI args (or fall back to interactive prompts)
    2. Build ScrapingFilters from choices
    3. Save selection to .tesserae_filters.json
    4. Optionally run the orchestrator
    5. Print summary
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # ── Keyword parsing: support JSON array syntax for developers ───────────
    # Accept: "python developer hiring" or '["python developer", "backend"]'
    raw_keywords = args.keywords
    if raw_keywords and raw_keywords.strip().startswith("[") and raw_keywords.strip().endswith("]"):
        try:
            parsed = json.loads(raw_keywords.strip())
            if isinstance(parsed, list) and all(isinstance(k, str) for k in parsed):
                args.keywords = [k.strip() for k in parsed if k.strip()]
            else:
                args.keywords = raw_keywords.split()
        except json.JSONDecodeError:
            args.keywords = raw_keywords.split()
    # └─────────────────────────────────────────────────────────────────────────

    # ── Step 1: Load last selection if no CLI args provided ──────────────
    last = _load_last_selection()

    # Determine run mode
    run_mode = args.run_mode
    if not run_mode:
        # Interactive mode: prompt for run mode
        print("\nKeyword run mode:")
        print("  1. Run existing keywords — use the provided keywords as-is")
        print("  2. Generate + run — generate new keywords via AI, then run them")
        mode_choice = input("Select mode (1-2, default 2): ").strip()
        if mode_choice == "1":
            run_mode = "existing"
        else:
            run_mode = "generate"

    # ── Step 2: Build ScrapingFilters ────────────────────────────────────
    # If CLI args were provided, use them directly (with last selection as fallback)
    # If no CLI filter args, fall back to interactive prompts

    cli_filter_args = any([
        args.category,
        args.delivery_status,
        args.platforms,
        args.media_type,
        args.date_preset,
        args.keywords,
        args.ad_age,
        args.regions,
        args.languages,
        args.sort_order,
        args.run_mode,
        args.cycles,
    ])

    if cli_filter_args:
        # User provided CLI filter args — use them, fall back to last selection for missing
        filters = build_scraping_filters(
            run_mode=run_mode,
            category=args.category or (last.get("category") if last else None),
            delivery_status=args.delivery_status or (last.get("delivery_status") if last else None),
            platforms=args.platforms or (last.get("platforms") if last else None),
            media_type=args.media_type or (last.get("media_type") if last else None),
            date_preset=args.date_preset or (last.get("date_preset") if last else None),
            keywords=args.keywords or (last.get("keywords") if last else None),
            ad_age=args.ad_age if args.ad_age is not None else (last.get("ad_age_hours") if last else None),
            regions=args.regions or (last.get("delivered_to_regions") if last else None),
            languages=args.languages or (last.get("languages") if last else None),
            sort_order=args.sort_order or (last.get("sort_order") if last else None),
            heuristic_patterns=last.get("heuristic_patterns") if last else None,
        )
    else:
        # No CLI filter args — run interactive selection
        print("\n" + "=" * 60)
        print("🔧 Tesserae — Interactive Filter Selection")
        print("=" * 60)

        # Quick summary of last selection
        if last:
            print("\nLast selection:")
            for key, value in last.items():
                if value:
                    print(f"  {key}: {value}")
            confirm = input("\nUse last selection? (y/n, default y): ").strip().lower()
            if confirm != "n":
                # Build filters from last selection
                filters = build_scraping_filters(
                    run_mode=run_mode,
                    category=last.get("category"),
                    delivery_status=last.get("delivery_status"),
                    platforms=last.get("platforms"),
                    media_type=last.get("media_type"),
                    date_preset=last.get("date_preset"),
                    keywords=last.get("keywords"),
                    ad_age=last.get("ad_age_hours"),
                    regions=last.get("delivered_to_regions"),
                    languages=last.get("languages"),
                    sort_order=last.get("sort_order"),
                    heuristic_patterns=last.get("heuristic_patterns"),
                )
            else:
                filters = build_scraping_filters(run_mode=run_mode)
        else:
            # First run ever — interactive from scratch
            filters = build_scraping_filters(run_mode=run_mode)

    # ── Step 3: Save preferences ─────────────────────────────────────────
    # Save the filter choices for next time
    selection = {
        "category": filters.category,
        "delivery_status": filters.delivery_status,
        "platforms": filters.platforms,
        "media_type": filters.media_type,
        "date_preset": filters.date_preset,
        "keywords": filters.keywords,
        "ad_age_hours": filters.ad_age_hours,
        "delivered_to_regions": filters.delivered_to_regions,
        "languages": filters.languages,
        "sort_order": filters.sort_order,
        "heuristic_patterns": {
            "require_ad_text_patterns": getattr(filters, "require_ad_text_patterns", []),
            "require_any_ad_text_patterns": getattr(filters, "require_any_ad_text_patterns", []),
            "exclude_ad_text_patterns": getattr(filters, "exclude_ad_text_patterns", []),
        },
    }
    _save_last_selection(selection)

    # ── Step 4: Run orchestrator (or preview only) ───────────────────────
    if args.preview:
        # Save preferences even in preview mode
        selection = {
            "category": filters.category,
            "delivery_status": filters.delivery_status,
            "platforms": filters.platforms,
            "media_type": filters.media_type,
            "date_preset": filters.date_preset,
            "keywords": filters.keywords,
            "ad_age_hours": filters.ad_age_hours,
            "delivered_to_regions": filters.delivered_to_regions,
            "languages": filters.languages,
            "sort_order": filters.sort_order,
            "heuristic_patterns": {
                "require_ad_text_patterns": getattr(filters, "require_ad_text_patterns", []),
                "require_any_ad_text_patterns": getattr(filters, "require_any_ad_text_patterns", []),
                "exclude_ad_text_patterns": getattr(filters, "exclude_ad_text_patterns", []),
            },
        }
        _save_last_selection(selection)

        print("\n" + "=" * 60)
        print("📋 Filters selected (preview mode — orchestrator not run)")
        print("=" * 60)
        print(f"  Category:        {filters.category}")
        print(f"  Keywords:        {filters.keywords or '(none)'}")
        print(f"  Run mode:        {run_mode}")
        print(f"  Delivery status: {filters.delivery_status}")
        print(f"  Platforms:       {', '.join(filters.platforms) if filters.platforms else 'all'}")
        print(f"  Media type:      {filters.media_type}")
        print(f"  Date preset:     {filters.date_preset}")
        print(f"  Ad age hours:    {filters.ad_age_hours or 'none'}")
        print(f"  Regions:         {', '.join(filters.delivered_to_regions) if filters.delivered_to_regions else 'ALL'}")
        print(f"  Languages:       {', '.join(filters.languages) if filters.languages else 'all'}")
        print(f"  Sort order:      {filters.sort_order}")
        print("=" * 60)
        print("Run again without --preview to start the orchestrator.")
        return

    # Run the orchestrator
    print("\n" + "=" * 60)
    print("🚀 Starting Tesserae orchestrator...")
    print("=" * 60)

    summary = asyncio.run(_run_with_filters(filters, args.cycles, run_mode))

    print("\n" + "=" * 60)
    print("🌐 Tesserae orchestrator run complete")
    print(f"🔁 Cycles: {summary['cycles']} | keywords searched: {summary['keywords_searched']} "
          f"| relevant ads: {summary['relevant_ads']}")
    print("=" * 60)

    # Save updated preferences after run (with any new keywords/etc)
    # The filters object may not have all the updated state, but we save what we have
    _save_last_selection(selection)


# ── Allow running as `python -m interface.orchestrate` ──────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    main()