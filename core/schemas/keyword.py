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

    The accumulation is the point. A keyword that found leads on Monday and
    nothing on Tuesday has still found leads: it has a life, not a snapshot.
    A one-cycle record could not tell those two apart, which made "proven"
    mean "lucky last Tuesday" and left the judge of keyword quality guessing.
    """

    keyword: str
    category: str | None = None
    runs_searched: int = 0
    ads_fetched: int = 0        # survived the heuristic filters
    relevant_ads: int = 0       # survived the AI relevance judge
    last_relevant_hit_at: datetime | None = None
    status_hint: KeywordStatus = KeywordStatus.new
    # When this keyword may be searched again — Actor 2's cooldown decision,
    # resolved to an absolute time by the script.
    #
    # Held inside the record rather than left to Redis expiry, because search
    # eligibility and the evidence are different questions and must expire
    # differently: a keyword stops being worth searching on schedule, but what
    # it returned is worth remembering until it is forgotten deliberately.
    reuse_at: datetime | None = None
    # When this keyword was last searched. Every keyword in one cycle is stamped
    # with the SAME instant, which is what identifies "the last batch" without a
    # separate batch id: the newest stamp is the last batch, and everything
    # sharing it is that batch.
    #
    # The generator is shown both its latest batch and its proven performers so
    # it can see the gap between where it is and where it should be. That
    # comparison is the whole point — a leaderboard alone cannot show drift.
    last_searched_at: datetime | None = None


class KeywordGenerationRequest(BaseModel):
    """What the script hands the GENERATOR (Actor 1)."""

    category_description: str
    user_profile_text: str                    # about-us + additional filters
    exclusion_prompts: list[str] = Field(default_factory=list)
    # Full keyword history, stats included. Not a bare list of strings: a name
    # the generator has already avoided teaches it nothing, whereas the same
    # name with "0 ads found" teaches it exactly what it got wrong. The stats
    # also carry the proven performers, which is the only feedback that tells
    # the generator which SHAPE of keyword actually reaches advertisers.
    searched_keywords: list[KeywordStats] = Field(default_factory=list)
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
