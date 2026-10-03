"""
Coverage checkpoint schemas (checkpoint c) + exclusion-prompt resources.

Approved design, invariants:
  - The reviewer may produce ONE OR MORE exclusion prompts. A prompt is
    built ONLY from keywords that together cover one skill/subskill.
  - Keywords NOT mapped onto any prompt are NOT replaced — they stay in
    the collection exactly as they are.
  - The composing keywords of a prompt are DELETED (Redis + collection):
    the prompt is the compressed stand-in. (Merged-in-place is wrong —
    deletion is the bloat solution.)
  - Existing prompts participate in coverage judging but are never
    rewritten by a later review; they expire by their own TTL.
  - all_covered=True (explicit confidence) -> harvest mode: re-search
    alive keywords for newly posted ads until prompts/keywords expire.
  - Each prompt gets its own timing from a SEPARATE actor call
    (parallel across prompts): prompt + composing keywords' raw stats
    -> RELATIVE days -> script computes absolute TTL.
"""

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from .keyword import KeywordStats


class KeywordDisposition(str, Enum):
    reuse_soon = "reuse_soon"  # productive — short cooldown, back in rotation fast
    cool_down = "cool_down"    # spent for now — default cooldown
    retire = "retire"          # exhausted — long cooldown before one more chance


class PromptTimingRequest(BaseModel):
    """What the script hands the PROMPT TIMING ACTOR — per prompt, in parallel."""

    exclusion_prompt: str
    composing_keywords: list[KeywordStats]  # raw stats of exactly its keywords


class PromptTiming(BaseModel):
    # RELATIVE days before this prompt expires and its territory becomes
    # searchable again. Script converts to absolute TTL.
    expire_after_days: int
    reason: str = ""


class PoolReviewRequest(BaseModel):
    """What the script hands the COVERAGE REVIEWER (checkpoint c)."""

    user_profile_text: str                 # about-us + additional filters
    keywords: list[KeywordStats]           # the live searched-keyword collection
    exclusion_prompts: list[str]           # prompts already in the collection
    pool_limit: int


class ExclusionPrompt(BaseModel):
    """One prompt capturing one skill/subskill territory."""

    prompt: str                            # e.g. "python automation specialist roles"
    covered_keywords: list[str]            # EXACTLY the keywords it replaces
    # Disposition per composing keyword's productivity — stats-driven.
    disposition: KeywordDisposition = KeywordDisposition.cool_down


class PoolReviewResult(BaseModel):
    prompts: list[ExclusionPrompt] = Field(default_factory=list)
    focus_guidance: str | None = None  # what the next generations should target
    all_covered: bool = False          # explicit confidence: everything covered
    # Required whenever all_covered is True (enforced below): how long to
    # stay in harvest mode before generation reopens. Relative days —
    # the script computes the absolute timestamp.
    recheck_after_days: int | None = None
    reasoning: str = ""

    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _recheck_required_when_covered(self):
        if self.all_covered and self.recheck_after_days is None:
            raise ValueError(
                "recheck_after_days is required when all_covered=True — the "
                "review must say how long harvest mode lasts"
            )
        return self
