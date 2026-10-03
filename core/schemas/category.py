"""
Category — the lens the whole pipeline looks through.

One category bundles the three things the pipeline parametrizes:
  - description   -> the intent sentence for checkpoint (a) — and, with
                     the About-you section later, the seed for
                     checkpoint (b) keyword generation
  - heuristics    -> the cheap first sieve's tick/cross patterns,
                     per category
  - id            -> scopes Redis verdict keys (tesserae:seen_ad:<id>:<ad>)
                     and the on-disk store, so one category's judgements
                     never interfere with another's

The user picks exactly one per run (single-select — the frontend makes
this a dropdown later; for now the registry holds the default only).
"""

from pydantic import BaseModel, Field


class CategoryHeuristics(BaseModel):
    """Per-category tick/cross patterns for the cheap first sieve."""

    require_ad_text_patterns: list[str] = Field(default_factory=list)      # AND
    require_any_ad_text_patterns: list[str] = Field(default_factory=list)  # OR
    exclude_ad_text_patterns: list[str] = Field(default_factory=list)


class Category(BaseModel):
    id: str            # url/redis/store-safe slug, e.g. "career_jobs"
    label: str         # human-readable name for the future UI
    description: str   # the intent the AI judges against
    heuristics: CategoryHeuristics

    @property
    def filter_kwargs(self) -> dict:
        """Heuristic patterns, ready to splat into ScrapingFilters."""
        return self.heuristics.model_dump()


# The category registry — the frontend's option list, one entry today.
CATEGORIES: dict[str, Category] = {
    "career_jobs": Category(
        id="career_jobs",
        label="Career & Job Opportunities",
        description=(
            "The user is a skilled professional who wants to earn "
            "through their skills — via jobs, employment, projects or "
            "freelance work. A relevant ad actively OFFERS such an "
            "opportunity: a company announcing openings or a "
            "recruitment/placement drive, a walk-in hiring event, an "
            "agency recruiting staff for clients, internships, "
            "freelance or contract project work. It is NOT relevant "
            "when career-flavored words are merely bait to sell "
            "something else — courses, bootcamps, certifications, "
            "webinars, coaching, CV-writing services, job boards or "
            "productivity tools. Judge what the ad OFFERS, not the "
            "words it borrows."
        ),
        heuristics=CategoryHeuristics(
            require_ad_text_patterns=["hiring"],
            exclude_ad_text_patterns=["course", "enroll"],
        ),
    ),
}

DEFAULT_CATEGORY_ID = "career_jobs"
