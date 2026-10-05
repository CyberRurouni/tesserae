"""
family.py — the profile/request family model.

A FAMILY is one declared professional objective. "Python developer" and
"Backend developer" are separate families even though both are backend work,
because their ACCEPTABLE AD SETS differ: the Python family rejects Node.js ads,
the Backend family does not.

Two trees, deliberately separate:

  Family         — who you ARE. Governs accept-vs-reject. Bound to the
                   profile text. Changing it can invalidate every verdict.
  RequestFamily  — what you want RIGHT NOW. Bound to a single Family.
                   Governs accept-vs-defer. Never changes what is "you".

The classification verdicts below are the only place the model is allowed to
make a judgement call about an edit. Everything else is bookkeeping.
"""

from typing import Literal

from pydantic import BaseModel, Field

# How an edit changes the acceptable set of ads:
#   none      — same ads acceptable, only worded better. Stays in the node.
#   narrowing — the new text accepts a SUBSET. Ads that were outside before
#               stay outside; accepted ads may now be out of scope.
#   widening  — the new text accepts a SUPERSET. Ads previously rejected or
#               deferred may now qualify, so they are re-judged.
ChangeKind = Literal["none", "narrowing", "widening"]


class FamilyClassification(BaseModel):
    """Verdict on an edit to a family's profile text."""

    change: ChangeKind
    new_family_required: bool
    # The family's identity, in the user's own words ("Python", "Backend
    # development"). Distinct families have distinct about values.
    about: str = ""
    # Coverage inside the fixed taxonomy: one domain, one OR MORE
    # specialisations. A JavaScript family legitimately covers Frontend AND
    # Backend at once, so a single leaf would hide it from a Backend family.
    category_domain: str = ""
    specialisations: list[str] = Field(default_factory=list)
    # Shown to the user when a new node is proposed. They decline => no save.
    explanation: str = ""


class RequestClassification(BaseModel):
    """Verdict on an edit to a family's additional request."""

    change: ChangeKind
    new_family_required: bool
    explanation: str = ""


class ScopeCheck(BaseModel):
    """
    Zero-creativity classification: is this ad inside this family's scope?

    Deliberately a bare yes/no with a fixed vocabulary. This is the call that
    decides whether an inherited verdict carries into a new node, so it must
    not be creative — see the deferred-never-rejected rule in
    modules/families/seed.py.
    """

    in_scope: bool
    matched_specialisation: str = ""
    reason: str = ""


class Family(BaseModel):
    """One professional objective, and the verdicts it inherits."""

    family_id: str
    about: str
    category_domain: str = ""
    # One or more. A family may legitimately span several.
    specialisations: list[str] = Field(default_factory=list)
    # The text as it stands. Edited in place when a change is cosmetic.
    profile_text: str
    # The text that founded the node. Kept so silent family drift can be
    # audited later; deliberately NOT used to re-check anything yet (MVP).
    founding_text: str
    # Ancestors whose verdicts this node inherits. Seeded, never copied —
    # a narrowing node can name the same ancestor as its own parent.
    seeded_from: list[str] = Field(default_factory=list)
    generation: int = 0
    created_at: str = ""
    updated_at: str = ""


class RequestFamily(BaseModel):
    """What the user wants right now, scoped to exactly one Family."""

    request_family_id: str
    family_id: str
    request_text: str
    founding_text: str
    seeded_from: list[str] = Field(default_factory=list)
    generation: int = 0
    created_at: str = ""
    updated_at: str = ""