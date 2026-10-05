"""
classifier.py — the only place a model is allowed to judge an edit.

Three separate calls, deliberately not one:

  classify_profile_change — is this edit cosmetic, narrowing, or widening?
                             Also places the family in the fixed taxonomy.
  classify_request_change  — same question, for the additional request.
  check_scope              — is this ad inside this family's chain? Bare yes/no.

check_scope is kept apart from the others on purpose. It decides whether an
inherited verdict carries into a new node, so it runs at temperature 0 against
a closed vocabulary and is never allowed to be creative. Folding it into the
editing calls would let a confident-sounding guess quietly drop real leads.

Nothing here writes. Classification is a proposal; the caller confirms, then
the store is updated. That is what lets a user decline without side effects.
"""

import json
import logging

from core import (
    AIClient,
    Family,
    FamilyClassification,
    RequestClassification,
    ScopeCheck,
)

from . import taxonomy

logger = logging.getLogger(__name__)


def _json_hint(shape: str) -> str:
    return f"Answer with JSON only, shaped exactly like: {shape}"


def _chains_for_prompt() -> str:
    """Grouped by domain so the model copies a bare specialisation name."""
    return "\n".join(
        f"  {domain}:\n" + "\n".join(f'    - "{spec}"' for spec in specs)
        for domain, specs in taxonomy.SPECIALISATIONS.items()
    )


def _parse_specialisations(raw_value) -> list[str]:
    """
    Accept bare names, and tolerate the model echoing a whole chain.

    Observed in practice: asked for specialisations it returned
    ["Programming -> Backend development"]. Taking the last segment recovers the
    right value instead of discarding an otherwise good classification.
    """
    out: list[str] = []
    for item in raw_value or []:
        value = str(item).strip()
        if "->" in value:
            value = value.split("->")[-1].strip()
        if value and value not in out:
            out.append(value)
    return out


# ============================================================================
# 🔹 PROFILE EDIT — cosmetic / narrowing / widening + taxonomy placement
# ============================================================================
_PROFILE_SYSTEM = f"""\
You maintain FAMILIES for a job lead-generation system.

A FAMILY is one declared professional objective with a fixed acceptable set of
ads. "Python developer" and "Backend developer" are DIFFERENT families even
though both are backend work, because Python rejects Node.js ads and Backend
does not.

Compare the CURRENT family text against the EDITED text and decide what kind
of edit this is:

  "none"      — the acceptable set of ads is UNCHANGED. Only wording got
                better or more explicit. Stays in the same family.
  "narrowing" — the new text accepts a SUBSET of what the old one accepted.
  "widening"  — the new text accepts a SUPERSET.

The deciding question: does the edited text accept ads the current text would
have REJECTED, or stop accepting ads it would have taken?
  - accepts more  -> "widening"
  - accepts fewer -> "narrowing"
  - same ads      -> "none"

Critical nuance, do not get this wrong. Adding a technology that the family was
ALREADY open to is NOT a widening:
  - "backend developer, expertise in python" -> "backend developer, expertise in
    python and node js"  = "none". A backend family was already open to Node.js.
  - "python developer" -> "python and node js developer" = "widening". The Python
    family rejected Node.js ads; naming Node.js admits ads whose fate was already
    sealed. That is the destructive direction, and it needs a new node.

Also assign:
  - "about"     — what this family is ABOUT, in a few words ("Python",
                  "Backend development"). Distinct families have distinct
                  about values.
  - "category_domain"  — one domain from the CLOSED list below.
  - "specialisations"  — one OR MORE specialisations inside that domain. List
                  EVERY area this family genuinely covers, not just the main
                  one. A JavaScript developer covering frontend AND backend
                  must list both — listing only one would hide the family from a
                  "Backend development" family that should have found it.
                  Copy the specialisation name EXACTLY as written below, e.g.
                  "Backend development". Do NOT include the domain and do NOT
                  write it as a chain like "Programming -> Backend development".

Valid domains and their specialisations (copy the specialisation name only):
{_chains_for_prompt()}

new_family_required is true for "narrowing" and "widening", false for "none".

{_json_hint('{"change": "none|narrowing|widening", "new_family_required": true|false, '
            '"about": "<max 4 words>", "category_domain": "<domain>", '
            '"specialisations": ["<specialisation>", "..."], '
            '"explanation": "<max 40 words, plain English, shown to the user when a new node is proposed>"}')}
"""


async def classify_profile_change(current_text: str, edited_text: str) -> FamilyClassification:
    client = AIClient()
    raw = await client.blocking(
        [
            {"role": "system", "content": _PROFILE_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"CURRENT FAMILY TEXT:\n{current_text or '(empty — new family)'}\n\n"
                    f"EDITED TEXT:\n{edited_text or '(empty)'}"
                ),
            },
        ],
        max_tokens=700,
        temperature=0.0,  # a classification, not a creative act
    )

    domain = str(raw.get("category_domain") or "").strip()
    specs = _parse_specialisations(raw.get("specialisations"))
    result = FamilyClassification(
        change=raw.get("change") or "none",
        new_family_required=bool(raw.get("new_family_required")),
        about=str(raw.get("about") or "").strip(),
        category_domain=domain,
        specialisations=specs,
        explanation=str(raw.get("explanation") or "").strip(),
    )

    if not taxonomy.is_valid(domain, specs):
        # An off-taxonomy level would break related-family discovery, so the
        # SCOPE is discarded. The `about` label is independent and often
        # perfectly good, so it is kept — dropping it would lose a useful name
        # over a taxonomy nit.
        logger.warning(
            "⚠️ Classifier proposed off-taxonomy scope %r/%r — keeping the name, dropping the scope",
            domain, specs,
        )
        result.category_domain, result.specialisations = "", []
    logger.info(
        "🧬 Profile edit: %s (new node: %s) — about=%r %s/%s",
        result.change, result.new_family_required, result.about,
        result.category_domain, result.specialisations,
    )
    return result


# ============================================================================
# 🔹 REQUEST EDIT — cosmetic / narrowing / widening
# ============================================================================
_REQUEST_SYSTEM = f"""\
You maintain REQUEST FAMILIES for a job lead-generation system.

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

new_family_required is true for "narrowing" and "widening", false for "none".

{_json_hint('{"change": "none|narrowing|widening", "new_family_required": true|false, '
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
        new_family_required=bool(raw.get("new_family_required")),
        explanation=str(raw.get("explanation") or "").strip(),
    )
    logger.info(
        "🎯 Request edit: %s (new node: %s) — %s",
        result.change, result.new_family_required, result.explanation,
    )
    return result


# ============================================================================
# 🔹 SCOPE CHECK — zero creativity, bare membership
# ============================================================================
_SCOPE_SYSTEM = f"""\
You answer ONE yes/no question with no interpretation and no creativity.

QUESTION: does the described work fall INSIDE the given scope?

Valid scopes (each is a domain and a specialisation):
{_chains_for_prompt()}

Rules:
  - "in_scope" is true when the work a normal recruiter would file under that
    specialisation. Judge the work itself, not the ad's wording or seniority.
  - An ad mentioning a technology that sits OUTSIDE the specialisation is out of
    scope. A "Backend development" scope does not admit data-science work, and a
    "Data science" scope does not admit backend work.
  - Seniority, contract vs permanent, and remote-vs-onsite do NOT change scope.
  - Do not reason about whether the user would want it. Scope only.

{_json_hint('{"in_scope": true|false, "matched_specialisation": "<specialisation or empty>", '
            '"reason": "<max 15 words>"}')}
"""


async def check_scope(ad_description: str, family: Family) -> ScopeCheck:
    client = AIClient()
    scope = (
        f"{family.category_domain} -> {', '.join(family.specialisations)}"
        if family.category_domain and family.specialisations
        else "(unclassified)"
    )
    raw = await client.blocking(
        [
            {"role": "system", "content": _SCOPE_SYSTEM},
            {
                "role": "user",
                "content": f"SCOPE: {scope}\n\nWORK: {ad_description or '(no text)'}",
            },
        ],
        max_tokens=300,
        temperature=0.0,
    )
    result = ScopeCheck(
        in_scope=bool(raw.get("in_scope")),
        matched_specialisation=str(raw.get("matched_specialisation") or "").strip(),
        reason=str(raw.get("reason") or "").strip(),
    )
    return result