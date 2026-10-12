"""
profile.py — the profile/request model.

A PROFILE is one declared professional objective. "Python developer" and
"Backend developer" are separate profiles even though both are backend work,
because their ACCEPTABLE AD SETS differ: the Python profile rejects Node.js ads,
the Backend profile does not.

Two things, deliberately separate:

  Profile         — who you ARE. Governs accept-vs-reject. Bound to the
                    profile text. Changing it can invalidate every verdict.
  ProfileRequest  — what you want RIGHT NOW. Bound to a single Profile.
                    Governs accept-vs-defer. Never changes what is "you".

An earlier version of this model was a TREE of "families" that inherited
verdicts from ancestors and were found by related-family discovery. That is
gone: accepted and junk are now global sets and rejections are per-profile, so
there is nothing to inherit and nothing to discover. The flat two-level shape
below is all that survives, and the names follow the concept rather than the
history.

The classification verdicts below are the only place the model is allowed to
make a judgement call about an edit. Everything else is bookkeeping.
"""


from typing import Literal

from pydantic import BaseModel

# How an edit changes the acceptable set of ads:
#   none      — same ads acceptable, only worded better. Stays in the node.
#   narrowing — the new text accepts a SUBSET. Ads that were outside before
#               stay outside; accepted ads may now be out of scope.
#   widening  — the new text accepts a SUPERSET. Ads previously rejected or
#               deferred may now qualify, so they are re-judged.
ChangeKind = Literal["none", "narrowing", "widening"]


class ProfileClassification(BaseModel):
    """Verdict on an edit to a profile's profile text."""

    change: ChangeKind
    new_profile_required: bool
    # The profile's identity, in the user's own words ("Python", "Backend
    # developer"). Distinct profiles have distinct about values.
    about: str = ""
    # Shown to the user when a new node is proposed. They decline => no save.
    explanation: str = ""


class RequestClassification(BaseModel):
    """Verdict on an edit to a profile's additional request."""

    change: ChangeKind
    new_profile_required: bool
    explanation: str = ""


class Profile(BaseModel):
    """
    One professional objective.

    Deliberately minimal. An earlier version carried `seeded_from`,
    `generation`, `category_domain` and `specialisations` — all of which only
    existed to place the node in a lineage tree. With inheritance gone they had
    no reader, so they are gone too rather than left to look meaningful.
    """

    profile_id: str
    about: str
    # The text as it stands. Edited in place when a change is cosmetic.
    profile_text: str
    # The text that founded the profile. Kept so silent drift can be audited
    # later; deliberately NOT used to re-check anything yet.
    founding_text: str
    created_at: str = ""
    updated_at: str = ""


class ProfileRequest(BaseModel):
    """What the user wants right now, scoped to exactly one Profile."""

    request_id: str
    profile_id: str
    request_text: str
    founding_text: str
    created_at: str = ""
    updated_at: str = ""
