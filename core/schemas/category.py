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


# Words that mean "this ad is offering work", any one of which is enough.
#
# Deliberately generous. A false positive costs one AI call; a false negative
# costs the lead forever, because the ad never reaches the judge and so can
# never be accepted, deferred or even recorded. The list is biased towards
# letting ads through.
HIRING_VOCABULARY = [
    "hiring",
    "we're hiring",
    "we are hiring",
    "now hiring",
    "join our team",
    "join us",
    "apply now",
    "apply today",
    "applications open",
    "vacancy",
    "vacancies",
    "job opening",
    "job openings",
    "career opportunity",
    "opportunity available",
    "looking for a",
    "looking for an",
    "we're looking",
    "we are looking",
    "seeking a",
    "seeking an",
    "recruiting",
    "recruitment",
    "position available",
    "open position",
    "now accepting applications",
    "work with us",
    "freelance",
    "contract role",
    "internship",
]


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
            # OR, not AND. This used to be require_ad_text_patterns=["hiring"],
            # which meant an ad survived only if it contained that one exact
            # word. Real job ads say "join our team", "now accepting
            # applications", "vacancy", "opportunity available" — a single
            # captured run dropped 112 of 119 harvested ads before the AI was
            # given a chance to judge any of them, and the run reported zero.
            #
            # "Does this offer work" is a judgement, not a substring test, and
            # the AI checkpoint already makes it properly. What is left here is
            # a cheap pre-sieve: catch the obvious non-jobs (courses, bootcamps,
            # job boards) and let everything else through to be judged.
            require_any_ad_text_patterns=list(HIRING_VOCABULARY),
            exclude_ad_text_patterns=["course", "enroll"],
        ),
    ),
}

DEFAULT_CATEGORY_ID = "career_jobs"
