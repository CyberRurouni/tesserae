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

# WHY an ad was rejected. The split matters because the two kinds have
# completely different reach:
#
#   no_hiring  — nobody is being employed. A course, a bootcamp, a B2B
#                recruiting service, a job board, a podcast. True regardless of
#                who is searching, so it is remembered globally and that ad is
#                never scraped or judged again by anyone.
#   not_for_me — a real employer IS hiring, just not for this person. Only
#                meaningful against one profile, so it is remembered there and
#                another profile gets to judge the ad for itself.
RejectionKind = Literal["no_hiring", "not_for_me"]


class AdRelevanceVerdict(BaseModel):
    ad_archive_id: str
    outcome: AdOutcome
    # Only meaningful when outcome == "rejected". Defaults to the SAFE end:
    # an unclassified rejection is treated as profile-specific, because that
    # only costs a re-judgement, whereas wrongly calling something junk
    # discards it for good.
    rejection_kind: RejectionKind = "not_for_me"
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