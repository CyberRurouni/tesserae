"""
classifier.py — the only place a model is allowed to judge an edit.

Two separate calls, deliberately not one:

  classify_profile_change — is this edit cosmetic, narrowing, or widening?
  classify_request_change  — same question, for the additional request.

Nothing here writes. Classification is a proposal; the caller confirms, then
the store is updated. That is what lets a user decline without side effects.

This module used to place each profile in a fixed taxonomy so related profiles
could be discovered and share verdicts. That is gone — accepted and junk are
global and rejections are per-profile, so there is nothing left to discover —
and with it went the taxonomy validation. The dead check was still calling it,
which would have raised NameError on the first profile edit.
"""

import json
import logging

from core import (
    AIClient,
    Profile,
    ProfileClassification,
    RequestClassification,
)


logger = logging.getLogger(__name__)


def _json_hint(shape: str) -> str:
    return f"Answer with JSON only, shaped exactly like: {shape}"



# ============================================================================
# 🔹 PROFILE EDIT — cosmetic / narrowing / widening + taxonomy placement
# ============================================================================
_PROFILE_SYSTEM = f"""\
You maintain PROFILES for a job lead-generation system.

A PROFILE is one declared professional objective with a fixed acceptable set of
ads. "Python developer" and "Backend developer" are DIFFERENT profiles even
though both are backend work, because Python rejects Node.js ads and Backend
does not.

Compare the CURRENT profile text against the EDITED text and decide what kind
of edit this is:

  "none"      — the acceptable set of ads is UNCHANGED. Only wording got
                better or more explicit. Stays in the same profile.
  "narrowing" — the new text accepts a SUBSET of what the old one accepted.
  "widening"  — the new text accepts a SUPERSET.

The deciding question: does the edited text accept ads the current text would
have REJECTED, or stop accepting ads it would have taken?
  - accepts more  -> "widening"
  - accepts fewer -> "narrowing"
  - same ads      -> "none"

Critical nuance, do not get this wrong. Adding a technology that the profile was
ALREADY open to is NOT a widening:
  - "backend developer, expertise in python" -> "backend developer, expertise in
    python and node js"  = "none". A backend profile was already open to Node.js.
  - "python developer" -> "python and node js developer" = "widening". The Python
    profile rejected Node.js ads; naming Node.js admits ads whose fate was already
    sealed. That is the destructive direction, and it needs a new node.

Also assign "about": what this profile is ABOUT, in a few words ("Python
backend developer"). Distinct profiles have distinct about values.

new_profile_required is true for "narrowing" and "widening", false for "none".

{_json_hint('{"change": "none|narrowing|widening", "new_profile_required": true|false, '
            '"about": "<max 4 words>", '
            '"explanation": "<max 40 words, plain English, shown to the user when a new profile is proposed>"}')}
"""


async def classify_profile_change(current_text: str, edited_text: str) -> ProfileClassification:
    client = AIClient()
    raw = await client.blocking(
        [
            {"role": "system", "content": _PROFILE_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"CURRENT PROFILE TEXT:\n{current_text or '(empty — new profile)'}\n\n"
                    f"EDITED TEXT:\n{edited_text or '(empty)'}"
                ),
            },
        ],
        max_tokens=700,
        temperature=0.0,  # a classification, not a creative act
    )

    result = ProfileClassification(
        change=raw.get("change") or "none",
        new_profile_required=bool(raw.get("new_profile_required")),
        about=str(raw.get("about") or "").strip(),
        explanation=str(raw.get("explanation") or "").strip(),
    )
    logger.info(
        "🧬 Profile edit: %s (new profile required: %s) — about=%r",
        result.change, result.new_profile_required, result.about,
    )
    return result


# ============================================================================
# 🔹 REQUEST EDIT — cosmetic / narrowing / widening
# ============================================================================
_REQUEST_SYSTEM = f"""\
You maintain REQUESTS for a job lead-generation system.

A REQUEST says what the user wants RIGHT NOW. It never changes who they are —
only which in-scope ads are wanted at this moment versus held for later.

Compare the CURRENT request against the EDITED request:

  "none"      — the same ads are wanted now as before. Only wording changed.
  "narrowing" — the edited request wants a SUBSET of what the old one wanted.
  "widening"  — the edited request wants a SUPERSET.

Ask it as: does the edited request allow accepting ads the current request was
rejecting or holding back?
  - allows more  -> "widening"
  - allows fewer -> "narrowing"
  - the same     -> "none"

Being more specific is NOT necessarily a narrowing. "Python roles" ->
"Python roles, intermediate to senior" is "none" if the ads it accepts are the
same set. Judge the ACCEPTED AD SET, not the wording or the strictness of tone.
Adding a genuine constraint that excludes ads IS a narrowing.

new_profile_required is true for "narrowing" and "widening", false for "none".

{_json_hint('{"change": "none|narrowing|widening", "new_profile_required": true|false, '
            '"explanation": "<max 40 words, plain English>"}')}
"""


async def classify_request_change(current_text: str, edited_text: str) -> RequestClassification:
    client = AIClient()
    raw = await client.blocking(
        [
            {"role": "system", "content": _REQUEST_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"CURRENT REQUEST:\n{current_text or '(empty — new request)'}\n\n"
                    f"EDITED REQUEST:\n{edited_text or '(empty)'}"
                ),
            },
        ],
        max_tokens=500,
        temperature=0.0,
    )
    result = RequestClassification(
        change=raw.get("change") or "none",
        new_profile_required=bool(raw.get("new_profile_required")),
        explanation=str(raw.get("explanation") or "").strip(),
    )
    logger.info(
        "🎯 Request edit: %s (new node: %s) — %s",
        result.change, result.new_profile_required, result.explanation,
    )
    return result


