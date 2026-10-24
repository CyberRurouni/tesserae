from typing import Literal

from pydantic import BaseModel, Field

# The four states an ad can end up in.
#
#   accepted — inside the profile AND wanted under the current request
#   held     — inside the profile, but the judge's confidence did not clear
#              the user's strictness threshold. NOT trash and NOT a rejection:
#              it is stored with that confidence and reconsidered when the
#              threshold is lowered. A held ad has a stage (profile or request)
#              and the threshold it missed.
#   deferred — inside the profile, not wanted under the current request.
#              Useful to this person, just not now. NOT trash: it keeps its
#              place in the profile and is reconsidered when the request
#              changes. It must never be downgraded to `rejected` while it
#              stays in scope.
#   rejected — outside the profile's acceptable set. Final.
AdOutcome = Literal["accepted", "deferred", "rejected", "held"]

# Which judge reached the verdict. A profile-held ad only needs the request
# judge when it is promoted; a request-held ad can be promoted without AI.
JudgeStage = Literal["profile", "request"]

# Why an accepted ad is held. `very_low` is its own bucket because no slider can
# promote it: the slider floor is 0.5, and this ad is below even that.
HeldKind = Literal["borderline", "very_low"]

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
    # deferrals so a later profile can pre-filter candidates by subject before
    # spending a model call on them.
    ad_category: str = ""
    # Which request profile produced this verdict. A `deferred` ad is only
    # re-judged when the request profile changes, so this has to be recorded.
    request_id: str | None = None
    # Which judge decided this. Profile-stage held ads are request-blind; the
    # request judge only has to run when one is promoted.
    stage: JudgeStage | None = None
    # Populated only for `held`: the threshold the confidence missed, and which
    # UI bucket it belongs in.
    held_threshold: float | None = Field(default=None, ge=0.5, le=0.9)
    held_kind: HeldKind | None = None
    # True when the rejection is a CHECKPOINT FAILURE, not a model
    # judgement — such ads deserve another chance on a future run.
    ai_failed: bool = False

    @property
    def relevant(self) -> bool:
        """Convenience for callers that only care about the accept bit."""
        return self.outcome == "accepted"