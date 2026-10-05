"""
Run-level schemas — who the user is, what a single run looks like.

UserProfile stays deliberately loose: skills/subskills are optional *hints*
the user may paste (the About section usually already carries them). Skill
discovery is emergent — the coverage reviewer derives the mapping between
keywords and skill areas itself at review time.
"""

from datetime import time

from pydantic import BaseModel, Field

# Concurrency multiplies captcha exposure, so the ceiling is deliberately low.
MAX_PARALLEL_WORKERS = 3

from .filters import ScrapingFilters


class UserProfile(BaseModel):
    """Answers to 'Tell us about yourself' / 'What are you looking for'."""

    background: str
    skills: list[str] = Field(default_factory=list)  # optional hints
    subskills: dict[str, list[str]] = Field(default_factory=dict)  # optional hints
    looking_for: str | None = None
    constraints: str | None = None  # e.g. "exclude on-site jobs unless based in Islamabad"


class TimeWindow(BaseModel):
    """Optional user-set run window, e.g. only run 10:00–11:00."""

    start: time
    end: time


class RunConfig(BaseModel):
    profile: UserProfile
    filters: ScrapingFilters
    time_window: TimeWindow | None = None

    # Which family this run belongs to. Verdicts are recorded against the
    # family (and inherited from its ancestors), NOT against a hash of the
    # profile text — a hash silently rotated the whole key space on any edit.
    # None means "resolve the active family at start".
    family_id: str | None = None
    # Which request family governs accept-vs-defer for this run.
    request_family_id: str | None = None

    # How many keywords to crawl at once. 1 = sequential (original behaviour).
    # Above 1 each worker gets its own page; higher means faster and a higher
    # chance of a captcha, which is why it is a setting and not a constant.
    parallel_workers: int = Field(default=1, ge=1, le=MAX_PARALLEL_WORKERS)

    # Keyword lifecycle — collection-based (approved design).
    keyword_pool_limit: int = 25            # collection size that triggers the coverage review
    keywords_per_generation: int = 12       # the generator returns 10-15
    default_cooldown_seconds: int = 24 * 3600  # alive keywords: back in rotation daily

    # AI relevance checkpoint pacing.
    judge_batch_size: int = 10
    judge_max_concurrent: int = 5

    # Harvest mode (all skills covered): sleep until the recheck date
    # reopens generation.
    harvest_enabled: bool = True
