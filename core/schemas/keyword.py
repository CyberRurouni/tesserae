"""
Keyword checkpoint schemas (checkpoint b) + the collection resources.

Flow (approved design):
  1. GENERATOR (Actor 1): category description + about-us + exclusion
     prompts + already-searched keywords  ->  10-15 new keywords. No
     timestamps here — a keyword earns its timing only AFTER being
     searched and its stats collected.
  2. After the array is exhausted, the TIMING ASSIGNER (Actor 2) reads
     the array + raw stats and assigns a RELATIVE reuse delay per
     keyword (days). The script converts to an absolute timestamp ->
     Redis TTL (db3, keyword_broker).
  3. Only then does the keyword enter the collection (searched +
     timestamped — never before).
  4. When the collection hits the threshold, the COVERAGE REVIEWER
     (checkpoint c) maps keyword groups onto skills -> exclusion
     prompts. Composing keywords are deleted (Redis + collection);
     unmapped keywords stay untouched.
"""

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class KeywordStatus(str, Enum):
    new = "new"        # too few runs to classify
    alive = "alive"    # still yielding relevant ads
    dry = "dry"        # zero relevant ads across its recent runs


class KeywordStats(BaseModel):
    """
    Per-keyword performance — computed by the script, judged by the AI.
    Accumulated ACROSS every search of this keyword, not just the last.
    """

    keyword: str
    category: str | None = None
    runs_searched: int = 0
    ads_fetched: int = 0        # survived the heuristic filters
    relevant_ads: int = 0       # survived the AI relevance checkpoint
    last_relevant_hit_at: datetime | None = None
    status_hint: KeywordStatus = KeywordStatus.new


class KeywordGenerationRequest(BaseModel):
    """What the script hands the GENERATOR (Actor 1)."""

    category_description: str
    user_profile_text: str                    # about-us + additional filters
    exclusion_prompts: list[str] = Field(default_factory=list)
    searched_keywords: list[str] = Field(default_factory=list)  # incl. expired history
    target_count: int = 12                    # 10-15 per approved design


class KeywordBatch(BaseModel):
    """The GENERATOR's answer — an array of keywords, no timing attached."""

    keywords: list[str]
    notes: str | None = None  # the AI's short reasoning, kept for audit
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class KeywordTimingRequest(BaseModel):
    """What the script hands the TIMING ASSIGNER (Actor 2)."""

    keywords: list[KeywordStats]   # the just-exhausted array with its stats


class KeywordTiming(BaseModel):
    keyword: str
    # RELATIVE reuse delay in days — the script computes the absolute
    # timestamp and sets the Redis TTL. None = script default applies.
    reuse_after_days: int | None = None
    reason: str = ""


class KeywordTimingResult(BaseModel):
    timings: list[KeywordTiming]
