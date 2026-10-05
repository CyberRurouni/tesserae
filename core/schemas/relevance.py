from typing import Literal

from pydantic import BaseModel, Field

# The three states an ad can end up in.
#
#   accepted — inside the family AND wanted under the current request
#   deferred — inside the family, not wanted under the current request.
#              Useful to this person, just not now. NOT trash: it keeps its
#              place in the family and is reconsidered when the request
#              changes. It must never be downgraded to `rejected` while it
#              stays in scope.
#   rejected — outside the family's acceptable set. Final.
AdOutcome = Literal["accepted", "deferred", "rejected"]


class AdRelevanceVerdict(BaseModel):
    ad_archive_id: str
    outcome: AdOutcome
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason: str = ""
    # What the ad is about, as a short label. Recorded on rejections and
    # deferrals so a later family can pre-filter candidates by subject before
    # spending a model call on them.
    ad_category: str = ""
    # Which request family produced this verdict. A `deferred` ad is only
    # re-judged when the request family changes, so this has to be recorded.
    request_family_id: str | None = None
    # True when the rejection is a CHECKPOINT FAILURE, not a model
    # judgement — such ads deserve another chance on a future run.
    ai_failed: bool = False

    @property
    def relevant(self) -> bool:
        """Convenience for callers that only care about the accept bit."""
        return self.outcome == "accepted"