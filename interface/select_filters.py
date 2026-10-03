"""
select_filters.py — Tesserae filter selector.

Allows user to select Meta Ads Library filters interactively or via CLI.
Returns a ScrapingFilters model instance for use by the orchestrator/scraper.

Key features:
- Real-time interactive prompts with sensible defaults
- CLI argument support (--category, --delivery-status, etc.)
- All filters have examples/options, not free-form text entry only
- Keyword mode selection: run existing keywords OR generate+run
- Ad age with preset buttons (common values)
- Platform selection with "all" option
- Heuristic patterns with clear AND/OR/EXCLUDE semantics
- Saves last selection to .tesserae_filters.json for quick reuse
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, timezone

from core import (
    CATEGORIES,
    DEFAULT_CATEGORY_ID,
    ScrapingFilters,
    DeliveryStatus,
    Platform,
    MediaType,
    DatePreset,
    SortOrder,
)

logger = logging.getLogger(__name__)

# ── Persistence of last selection ──────────────────────────────────────────

LAST_SELECTION_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".tesserae_filters.json"
)

def _load_last_selection() -> dict | None:
    """Load last filter selection from disk, if it exists."""
    try:
        if os.path.exists(LAST_SELECTION_PATH):
            with open(LAST_SELECTION_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Failed to load last selection: %s", e)
    return None

def _save_last_selection(selection: dict) -> None:
    """Save filter selection to disk for quick reuse next time."""
    try:
        with open(LAST_SELECTION_PATH, "w", encoding="utf-8") as f:
            json.dump(selection, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning("Failed to save last selection: %s", e)


# ── CLI argument parser ────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tesserae — Select Meta Ads Library filters."
    )

    parser.add_argument("--category", type=str, default=None,
                        help="Category ID (default: last used or career_jobs)")
    parser.add_argument("--delivery-status", type=str, default=None,
                        choices=["all", "active", "inactive"],
                        help="Delivery status (default: last used or active)")
    parser.add_argument("--platforms", type=str, default=None,
                        help="Comma-separated platforms "
                             "(facebook,instagram,messenger,audience_network; default: all)")
    parser.add_argument("--media-type", type=str, default=None,
                        choices=["all", "image", "video", "meme"],
                        help="Media type (default: last used or all)")
    parser.add_argument("--date-preset", type=str, default=None,
                        choices=["any", "last_7_days", "last_30_days", "custom"],
                        help="Date preset (default: last used or any)")
    parser.add_argument("--keywords", type=str, default=None,
                        help="Space-separated keywords (overrides generation)")
    parser.add_argument("--ad-age", type=int, default=None,
                        help="Ad age in hours (presets: 24=day, 168=week, 720=month)")
    parser.add_argument("--regions", type=str, default=None,
                        help="Comma-separated ISO country codes (default: ALL)")
    parser.add_argument("--languages", type=str, default=None,
                        help="Comma-separated ISO 639-1 codes (default: all)")
    parser.add_argument("--sort-order", type=str, default=None,
                        choices=["relevance", "date"],
                        help="Sort order (default: last used or relevance)")
    parser.add_argument("--require-patterns", type=str, default=None,
                        help="AND patterns, comma-separated")
    parser.add_argument("--require-any-patterns", type=str, default=None,
                        help="OR patterns, comma-separated")
    parser.add_argument("--exclude-patterns", type=str, default=None,
                        help="Exclude patterns, comma-separated")
    parser.add_argument("--run-mode", type=str, default=None,
                        choices=["existing", "generate"],
                        help="Keyword run mode: 'existing' uses provided keywords, "
                             "'generate' generates then runs them")

    return parser


# ── Interactive prompt helpers ─────────────────────────────────────────────

def _prompt(text: str, default=None, options: list = None) -> str:
    """Print prompt and read a line from stdin.

    Args:
        text: Prompt description
        default: Default value (shown in parentheses)
        options: List of (number, label) tuples for constrained choice
    """
    if default is not None:
        full = f"{text} (default: {default}): "
    else:
        full = f"{text}: "

    if options:
        # Show option list
        full += "\n"
        for num, label in options:
            full += f"  {num}. {label}\n"
        full += " "

    try:
        return input(full).strip() or (default or "")
    except EOFError:
        return default or ""


def _prompt_choice(text: str, default_idx: int, options: list) -> int:
    """Prompt user to choose from options by number.

    Args:
        text: Prompt text
        default_idx: Default option index (0-based)
        options: List of strings to display
    """
    print("\n" + text)
    for i, opt in enumerate(options, 1):
        marker = " *" if i - 1 == default_idx else " "
        print(f"  {i}{marker}. {opt}")
    while True:
        choice = _prompt("Select number", str(default_idx + 1))
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return idx
        except ValueError:
            pass
        print("Invalid choice, try again.")


# ── Filter selection functions ─────────────────────────────────────────────

def _select_category() -> str:
    """Let user choose category, with last selection as default."""
    last = _load_last_selection()
    default_cat = last.get("category") if last else DEFAULT_CATEGORY_ID

    print("\nAvailable categories:")
    for i, (cid, cat) in enumerate(CATEGORIES.items(), 1):
        print(f"  {i}. {cat.label} ({cid})")
    choices = {str(i): cid for i in range(1, len(CATEGORIES) + 1)}

    # Pre-select last used if valid
    if str(default_cat) in choices:
        default_num = str(default_cat)  # the choice number maps to the cat id indirectly
        # Actually we need to find the number for the default category
        default_num = None
        for num, cid in choices.items():
            if cid == default_cat:
                default_num = num
                break
        choice = _prompt_choice("Select category number", default_num or 1,
                                [f"{num}. {cat.label}" for num, cat in CATEGORIES.items()])
        selected = choices.get(str(choice))
        if selected:
            return selected
        return default_cat

    # Fallback: just list and let user pick
    while True:
        choice = _prompt("Select category number", default=str(default_cat))
        if not choice and default_cat:
            return default_cat
        if choice in choices:
            return choices[choice]
        print("Invalid choice, try again.")


def _select_delivery_status() -> str:
    """Let user choose delivery status."""
    last = _load_last_selection()
    default_ds = last.get("delivery_status") if last else "active"

    print("\nDelivery status:")
    print("  1. all        — show all ads regardless of status")
    print("  2. active     — only ads currently running")
    print("  3. inactive   — only ads no longer running")
    options = ["all", "active", "inactive"]

    # Use last selection as default if valid
    if default_ds in options:
        default_idx = options.index(default_ds)
    else:
        default_idx = 1  # active

    choice_idx = _prompt_choice("Select delivery status", default_idx, options)
    return options[choice_idx]


def _select_platforms() -> list[str]:
    """Let user choose platforms, with 'all' as explicit option."""
    last = _load_last_selection()
    default_plats = last.get("platforms") if last else None

    print("\nPlatforms (select by number, or 'a' for all):")
    print("  1. facebook")
    print("  2. instagram")
    print("  3. messenger")
    print("  4. audience_network")
    print("  a. all")

    choices_map = {
        "1": Platform.facebook.value,
        "2": Platform.instagram.value,
        "3": Platform.messenger.value,
        "4": Platform.audience_network.value,
        "a": None,  # sentinel for "all"
    }

    # Build option list for prompt_choice
    option_labels = ["facebook", "instagram", "messenger", "audience_network", "all"]

    # Determine default
    default_is_all = default_plats is None or (isinstance(default_plats, list) and len(default_plats) == 0)
    if default_is_all:
        default_idx = 4  # "all" is option 5/index 4
    else:
        # Check if default_plats contains all
        if isinstance(default_plats, list) and set(p.value for p in Platform) == set(default_plats):
            default_idx = 4
        else:
            default_idx = 0  # no default, user picks

    # For simplicity, let user type their choices
    print("\nYour selection (comma-separated numbers or 'a' for all): ")
    raw = input().strip().lower()

    if raw == "a" or not raw:
        if default_is_all:
            return [p.value for p in Platform]
        # Fall through to ask again with better default
        return _select_platforms()  # recursive, but with better state

    selected = []
    for part in raw.split(","):
        part = part.strip()
        if part in choices_map:
            val = choices_map[part]
            if val is None:  # "all"
                return [p.value for p in Platform]
            selected.append(val)
        else:
            print(f"Invalid selection: {part}. Try again.")
            return _select_platforms()  # recursive retry

    # If user picked specific ones, return them (even if empty = all)
    return selected if selected else [p.value for p in Platform]


def _select_media_type() -> str:
    """Let user choose media type."""
    last = _load_last_selection()
    default_mt = last.get("media_type") if last else "all"

    print("\nMedia type:")
    print("  1. all")
    print("  2. image")
    print("  3. video")
    print("  4. meme")

    options = ["all", "image", "video", "meme"]
    if default_mt in options:
        default_idx = options.index(default_mt)
    else:
        default_idx = 0

    choice = _prompt("Select media type", default=str(default_mt))
    if not choice and default_mt:
        return default_mt
    if choice in ["1", "2", "3", "4"]:
        return options[int(choice) - 1]
    return default_mt or "all"


def _select_date_preset() -> str:
    """Let user choose date preset."""
    last = _load_last_selection()
    default_dp = last.get("date_preset") if last else "any"

    print("\nDate preset:")
    print("  1. any        — no date filter")
    print("  2. last_7_days — last 7 days")
    print("  3. last_30_days — last 30 days")
    print("  4. custom     — enter custom start/end dates")

    options = ["any", "last_7_days", "last_30_days", "custom"]
    if default_dp in options:
        default_idx = options.index(default_dp)
    else:
        default_idx = 0

    choice = _prompt("Select date preset", default=str(default_dp))
    if not choice and default_dp:
        return default_dp
    if choice in ["1", "2", "3", "4"]:
        return options[int(choice) - 1]
    return default_dp or "any"


def _select_ad_age_hours() -> int | None:
    """Let user choose ad age with preset buttons.

    Presets:
      24  = last 24 hours
      168 = last 7 days (1 week)
      720 = last 30 days (approx 1 month)
    """
    last = _load_last_selection()
    default_aa = last.get("ad_age_hours") if last else None

    print("\nAd age filter (how recent the ads should be):")
    print("  Presets (enter number):")
    print("    1. 24  hours   — last 24 hours")
    print("    2. 168 hours   — last 7 days (1 week)")
    print("    3. 720 hours   — last 30 days (approx 1 month)")
    print("    4. Custom      — enter your own hours")
    print("    5. None        — no ad-age filter")

    options = ["24 hours", "168 hours", "720 hours", "Custom", "None"]
    if default_aa is not None:
        # Find closest preset
        if default_aa == 24:
            default_idx = 0
        elif default_aa == 168:
            default_idx = 1
        elif default_aa == 720:
            default_idx = 2
        else:
            default_idx = 4  # Custom/None
    else:
        default_idx = 4  # Default to "None"

    choice = _prompt("Select ad age preset", default=str(default_idx + 1))
    try:
        pick = int(choice)
    except ValueError:
        pick = 1  # fallback

    if pick == 1:
        return 24
    elif pick == 2:
        return 168
    elif pick == 3:
        return 720
    elif pick == 4:
        # Custom
        custom = _prompt("Enter ad age in hours")
        try:
            v = int(custom)
            return v if v > 0 else None
        except (ValueError, TypeError):
            return None
    else:  # pick == 5 or None
        return None


def _select_keywords_run_mode() -> str:
    """Let user choose how to handle keywords.

    Returns:
        'existing' — use the provided keywords as-is
        'generate' — generate new keywords then run them
    """
    print("\nKeyword run mode:")
    print("  1. Run existing keywords — use the keywords you provide, "
          "no AI generation")
    print("  2. Generate + run — generate new keywords via AI, then run them")

    options = ["Run existing keywords", "Generate + run"]
    default_idx = 0  # first option is default

    choice = _prompt("Select mode", default="1")
    if choice in ["1", "2"]:
        return options[int(choice) - 1]
    return options[default_idx]


def _parse_keywords_input(user_input: str) -> list[str]:
    """Parse keyword input, supporting JSON array syntax for developers.

    Examples accepted:
      - "python developer hiring"          -> ['python', 'developer', 'hiring'] (space-separated)
      - ["python developer", "backend"]    -> ['python developer', 'backend'] (JSON array)
      - "python,developer,hiring"          -> ['python', 'developer', 'hiring'] (comma-separated)
    """
    user_input = user_input.strip()
    if not user_input:
        return []

    # Try JSON array syntax first (for developer-friendly input)
    if user_input.startswith("[") and user_input.endswith("]"):
        try:
            import json
            parsed = json.loads(user_input)
            if isinstance(parsed, list) and all(isinstance(k, str) for k in parsed):
                # Strip whitespace from each keyword
                return [k.strip() for k in parsed if k.strip()]
            # If JSON parse succeeded but not a list of strings, fall through
        except (json.JSONDecodeError, ValueError):
            pass  # Fall through to other parsing methods

    # Fall back to space-separated (original behavior)
    # But also try comma-separated as a middle ground
    if "," in user_input and " " not in user_input:
        return [k.strip() for k in user_input.split(",") if k.strip()]

    # Default: space-separated
    return [k.strip() for k in user_input.split() if k.strip()]


def _select_keywords(mode: str = "existing") -> list[str]:
    """Let user enter keywords, mode-dependent."""
    if mode == "existing":
        print("\nEnter keywords (press Enter for none):")
        print("  Examples:")
        print("    • python developer hiring       (space-separated, 3 keywords)")
        print('    • ["python developer", "backend"]  (JSON array, 2 keywords)')
        print("    • python,developer,hiring       (comma-separated, 3 keywords)")
        kw_input = input("> ").strip()
        return _parse_keywords_input(kw_input)
    else:  # generate
        print("\nKeyword generation will be handled by the AI orchestrator.")
        print("No manual keyword entry needed in this mode.")
        return []


def _select_languages() -> list[str]:
    """Let user choose languages."""
    last = _load_last_selection()
    default_langs = last.get("languages") if last else None

    print("\nLanguages (ISO 639-1 codes, comma-separated, or 'a' for all):")
    print("  e.g. en, es, ur  |  'a' for all languages")

    raw = _prompt("Languages", default="a" if not default_langs else ", ".join(default_langs))
    if not raw and default_langs:
        return default_langs

    if raw.lower() == "a" or not raw:
        return []

    return [l.strip().lower() for l in raw.split(",") if l.strip()]


def _select_sort_order() -> str:
    """Let user choose sort order."""
    last = _load_last_selection()
    default_so = last.get("sort_order") if last else "relevance"

    print("\nSort order:")
    print("  1. relevance   — sort by relevance to keyword")
    print("  2. date        — sort by ad date (newest first)")

    options = ["relevance", "date"]
    if default_so in options:
        default_idx = options.index(default_so)
    else:
        default_idx = 0

    choice = _prompt("Select sort order", default=str(default_idx + 1))
    if not choice and default_so:
        return default_so
    if choice in ["1", "2"]:
        return options[int(choice) - 1]
    return default_so or "relevance"


def _select_heuristic_patterns_last() -> dict | None:
    """Load last heuristic patterns from saved selection, or return None."""
    last = _load_last_selection()
    if last is None:
        return None
    return last.get("heuristic_patterns")


def _select_heuristic_patterns() -> dict[str, list[str]]:
    """Let user enter heuristic patterns with clear explanations.

    Returns dict with keys:
      require_ad_text_patterns:   AND — ad MUST contain ALL these patterns
      require_any_ad_text_patterns: OR — ad MUST contain AT LEAST ONE of these
      exclude_ad_text_patterns:   — ad MUST NOT contain ANY of these
    """
    # Try to load last selection first
    last_pats = _select_heuristic_patterns_last()

    print("\nHeuristic patterns (cheap first-sieve before AI judging):")
    print("  AND semantics (REQUIRE): ad TEXT MUST contain ALL of these patterns")
    print("    e.g. 'hiring' — ad must mention hiring")
    print()
    print("  OR semantics (REQUIRE-ANY): ad TEXT MUST contain AT LEAST ONE of these patterns")
    print("    e.g. 'walk-in' — ad must mention walk-in somewhere")
    print()
    print("  EXCLUDE semantics: ad TEXT MUST NOT contain ANY of these patterns")
    print("    e.g. 'course' — ad promoting a course is excluded")
    print()

    if last_pats:
        require_default = ", ".join(last_pats.get("require_ad_text_patterns", []))
        require_any_default = ", ".join(last_pats.get("require_any_ad_text_patterns", []))
        exclude_default = ", ".join(last_pats.get("exclude_ad_text_patterns", []))
    else:
        require_default = ""
        require_any_default = ""
        exclude_default = ""

    require = _prompt("AND patterns (comma-separated, e.g. hiring, walk-in)",
                      default=require_default)
    require_any = _prompt("OR patterns (comma-separated, e.g. walk-in, hiring)",
                          default=require_any_default)
    exclude = _prompt("EXCLUDE patterns (comma-separated, e.g. course, enrollment)",
                      default=exclude_default)

    result = {}
    if require.strip():
        result["require_ad_text_patterns"] = [p.strip() for p in require.split(",") if p.strip()]
    if require_any.strip():
        result["require_any_ad_text_patterns"] = [p.strip() for p in require_any.split(",") if p.strip()]
    if exclude.strip():
        result["exclude_ad_text_patterns"] = [p.strip() for p in exclude.split(",") if p.strip()]

    return result


# ── Build ScrapingFilters ──────────────────────────────────────────────────

def build_scraping_filters(
    run_mode: str = "existing",
    non_interactive: bool = False,
    **kwargs,
) -> ScrapingFilters:
    """Build a ScrapingFilters instance from collected choices.

    Args:
        run_mode: 'existing' or 'generate' — how to handle keywords
        non_interactive: when True, never prompt on stdin; any value the caller
            omitted falls back to a safe default. Required for API/background
            runs, otherwise a missing value blocks waiting for terminal input.
        **kwargs: individual filter values (overrides interactive prompts)

    Returns:
        ScrapingFilters model instance ready for use.
    """

    def pick(value, prompt):
        """Return the given value, or prompt for it unless we are non-interactive."""
        if value:
            return value
        if non_interactive:
            return None
        return prompt()

    # ──── Category ────
    category_id = pick(kwargs.get("category"), _select_category)
    if not category_id:
        category_id = DEFAULT_CATEGORY_ID

    category = CATEGORIES.get(category_id, CATEGORIES[DEFAULT_CATEGORY_ID])

    # ──── Delivery status ────
    delivery_status = pick(kwargs.get("delivery_status"), _select_delivery_status)
    if not delivery_status:
        delivery_status = "active"

    # ──── Platforms ────
    platforms_arg = kwargs.get("platforms")
    platforms = None
    if platforms_arg:
        # Handle both string (CLI) and list (API / last selection)
        if isinstance(platforms_arg, list):
            platforms = [p.strip() for p in platforms_arg if p.strip()]
        else:
            platforms = [p.strip() for p in platforms_arg.split(",") if p.strip()]
        valid = {p.value for p in Platform}
        platforms = [p for p in platforms if p in valid] or [p.value for p in Platform]
    elif not non_interactive:
        platforms = _select_platforms()
    else:
        platforms = [p.value for p in Platform]

    # ──── Media type ────
    media_type = pick(kwargs.get("media_type"), _select_media_type)
    if not media_type:
        media_type = "all"

    # ──── Date preset ────
    date_preset = pick(kwargs.get("date_preset"), _select_date_preset)
    if not date_preset:
        date_preset = "any"
    custom_start = None if date_preset != "custom" else date.today()
    custom_end = None

    # ──── Keywords ────
    keywords = kwargs.get("keywords")
    if keywords:
        if isinstance(keywords, list):
            keywords = [k.strip() for k in keywords if k.strip()]
        else:
            # Single string — treat as one keyword so phrases survive
            keywords = [keywords.strip()] if keywords.strip() else []
    elif non_interactive:
        keywords = []
    else:
        keywords = _select_keywords(run_mode)

    # ──── Ad age ────
    ad_age_hours = kwargs.get("ad_age")
    if ad_age_hours is None and not non_interactive:
        ad_age_hours = _select_ad_age_hours()

    # ──── Results limit — removed per user request, default only ────
    # (Kept at model default of 50, no prompt)

# ──── Regions ────
    regions_arg = kwargs.get("regions")
    delivered_regions = None
    if regions_arg:
        if isinstance(regions_arg, list):
            delivered_regions = [r.strip().upper() for r in regions_arg if r.strip()]
        else:
            delivered_regions = [r.strip().upper() for r in regions_arg.split(",") if r.strip()]
        if not delivered_regions:
            delivered_regions = ["ALL"]
    elif non_interactive:
        delivered_regions = ["ALL"]
    else:
        regions_raw = _prompt("Delivered regions (ISO codes, comma-separated, or 'a' for ALL)",
                              default="a")
        if regions_raw.lower() == "a" or not regions_raw:
            delivered_regions = ["ALL"]
        else:
            delivered_regions = [r.strip().upper() for r in regions_raw.split(",") if r.strip()]

    # ──── Languages ────
    languages_arg = kwargs.get("languages")
    if languages_arg:
        if isinstance(languages_arg, list):
            languages = [l.strip().lower() for l in languages_arg if l.strip()]
        else:
            languages = [l.strip().lower() for l in languages_arg.split(",") if l.strip()]
        if not languages:
            languages = []
    elif non_interactive:
        languages = []
    else:
        languages = _select_languages()

    # ──── Sort order ────
    sort_order = pick(kwargs.get("sort_order"), _select_sort_order)
    if not sort_order:
        sort_order = "relevance"

    # ──── Heuristic patterns ────
    heuristic_patterns = kwargs.get("heuristic_patterns")
    if not heuristic_patterns:
        heuristic_patterns = {} if non_interactive else _select_heuristic_patterns()

    # ──── Build ScrapingFilters ────
    filters = ScrapingFilters(
        category=category_id,
        keywords=keywords,
        delivery_status=delivery_status,
        platforms=platforms,
        media_type=media_type,
        date_preset=date_preset,
        custom_start_date=custom_start,
        custom_end_date=custom_end,
        delivered_to_regions=delivered_regions,
        languages=languages,
        sort_order=sort_order,
        ad_age_hours=ad_age_hours,
        results_limit_per_keyword=50,  # fixed default, per user request
    )

    # Apply heuristic patterns if provided
    if heuristic_patterns.get("require_ad_text_patterns"):
        filters.require_ad_text_patterns = heuristic_patterns["require_ad_text_patterns"]
    if heuristic_patterns.get("require_any_ad_text_patterns"):
        filters.require_any_ad_text_patterns = heuristic_patterns["require_any_ad_text_patterns"]
    if heuristic_patterns.get("exclude_ad_text_patterns"):
        filters.exclude_ad_text_patterns = heuristic_patterns["exclude_ad_text_patterns"]

    # Save selection for next time
    selection = {
        "category": category_id,
        "delivery_status": delivery_status,
        "platforms": platforms,
        "media_type": media_type,
        "date_preset": date_preset,
        "keywords": keywords,
        "ad_age_hours": ad_age_hours,
        "delivered_to_regions": delivered_regions,
        "languages": languages,
        "sort_order": sort_order,
        "heuristic_patterns": {
            "require_ad_text_patterns": getattr(filters, "require_ad_text_patterns", []),
            "require_any_ad_text_patterns": getattr(filters, "require_any_ad_text_patterns", []),
            "exclude_ad_text_patterns": getattr(filters, "exclude_ad_text_patterns", []),
        },
    }
    _save_last_selection(selection)

    return filters


# ── Smoke test ─────────────────────────────────────────────────────────────

async def _smoke_test(filters: ScrapingFilters) -> int:
    """Quick scrape test — returns number of ads found."""
    from modules.scraper import run_scrape
    try:
        ads = await run_scrape(filters, headless=False)
        return len(ads)
    except Exception as e:
        logger.warning("Smoke test failed: %s", e)
        return -1


# ── CLI entry point ────────────────────────────────────────────────────────

def main(argv: list | None = None) -> None:
    """Main entry point for the filter selector.

    One file to rule them all:
    - Selects all Tesserae filters interactively or via CLI
    - Returns ScrapingFilters ready for orchestrator/scraper
    - Saves last selection for quick reuse
    - Can run a quick smoke test scrape

    Usage:
        python3 select_filters.py              # interactive mode
        python3 select_filters.py --args       # CLI mode
        python3 select_filters.py --smoke      # run smoke test with last filters
    """
    parser = _build_parser()
    args = parser.parse_args(argv) if argv else parser.parse_args()

    # Determine run mode
    run_mode = args.run_mode
    if not run_mode:
        # Interactively select run mode if not specified
        run_mode = _select_keywords_run_mode()

    # Build filters — this collects any missing values interactively
    filters = build_scraping_filters(
        run_mode=run_mode,
        category=args.category,
        delivery_status=args.delivery_status,
        platforms=args.platforms,
        media_type=args.media_type,
        date_preset=args.date_preset,
        keywords=args.keywords,
        ad_age=args.ad_age,
        regions=args.regions,
        languages=args.languages,
        sort_order=args.sort_order,
        heuristic_patterns=args.require_patterns and {
            "require_ad_text_patterns": [p.strip() for p in args.require_patterns.split(",") if p.strip()],
        } or None,
    )

    print("\n" + "=" * 60)
    print("📋 Selected Filters Summary")
    print("=" * 60)
    print(f"  Category:        {filters.category}")
    print(f"  Keywords:        {filters.keywords or '(none)'}")
    print(f"  Run mode:        {run_mode}")
    print(f"  Delivery status: {filters.delivery_status}")
    print(f"  Platforms:       {', '.join(filters.platforms) if filters.platforms else 'all'}")
    print(f"  Media type:      {filters.media_type}")
    print(f"  Date preset:     {filters.date_preset}")
    print(f"  Ad age hours:    {filters.ad_age_hours or 'none'}")
    print(f"  Results limit:   {filters.results_limit_per_keyword} (fixed)")
    print(f"  Regions:         {', '.join(filters.delivered_to_regions) if filters.delivered_to_regions else 'ALL'}")
    print(f"  Languages:       {', '.join(filters.languages) if filters.languages else 'all'}")
    print(f"  Sort order:      {filters.sort_order}")
    if getattr(filters, "require_ad_text_patterns", None):
        print(f"  Require patterns: {', '.join(filters.require_ad_text_patterns)}")
    if getattr(filters, "require_any_ad_text_patterns", None):
        print(f"  Require-any:      {', '.join(filters.require_any_ad_text_patterns)}")
    if getattr(filters, "exclude_ad_text_patterns", None):
        print(f"  Exclude patterns: {', '.join(filters.exclude_ad_text_patterns)}")
    print("=" * 60)

    # Quick smoke test
    print("\n🚀 Running quick smoke test scrape...")
    accepted = asyncio.run(_smoke_test(filters))
    if accepted >= 0:
        print(f"✅ Scraped {accepted} ads returned")
    else:
        print("⚠️ Smoke test could not complete (browser/captcha). Filters are ready anyway.")
    print("=" * 60)
    print("🏁 Filters ready for orchestrator usage.")
    print("=" * 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    main()