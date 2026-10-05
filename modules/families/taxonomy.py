"""
taxonomy.py — the FIXED category vocabulary.

The family classifier is only allowed to CHOOSE from this tree, never invent
it. Free-form chains silently break related-family discovery: one family that
records "backend" and another that records "Backend development" never match,
and the seeding step quietly re-judges everything it was meant to skip.

Shape is domain -> specialisation, matching the chain shape the families
already use (Programming -> Full Stack Development -> Backend development).
"""

from typing import Iterable

DOMAIN: str = "Programming"

# Every specialisation Tesserae can currently place a family in. Adding a new
# one is a deliberate edit here, not an AI decision at runtime.
SPECIALISATIONS: dict[str, list[str]] = {
    "Programming": [
        "Full Stack Development",
        "Backend development",
        "Frontend development",
        "Mobile development",
        "Desktop development",
        "Data engineering",
        "Data science",
        "AI / Machine Learning",
        "DevOps / Infrastructure",
        "QA / Testing",
        "Security engineering",
    ],
    "Design": [
        "UI / UX design",
        "Graphic design",
        "Product design",
    ],
    "Marketing": [
        "Performance marketing",
        "Content marketing",
        "Social media marketing",
        "Marketing automation",
    ],
    "Operations": [
        "Business operations",
        "Customer support",
        "Project management",
    ],
}

# Cross-cutting focuses. Not a chain level — an ad can be Python AND
# automation-focused at once — so they are tracked separately and never used
# for family matching.
FOCUSES: list[str] = [
    "Remote-first",
    "Contract / freelance",
    "Part-time",
    "Lead / senior ownership",
    "Junior / entry level",
    "Internship",
]


def all_chains() -> list[list[str]]:
    """Every valid chain, longest last, for prompting and validation."""
    chains = [[domain, spec] for domain, specs in SPECIALISATIONS.items() for spec in specs]
    return sorted(chains, key=len)


def is_valid(domain: str, specialisations: Iterable[str]) -> bool:
    """
    A family covers ONE OR MORE specialisations inside ONE domain.

    Not a single leaf: a JavaScript family genuinely spans Frontend AND Backend
    at the same time, and forcing one specialisation per family would make it
    invisible to a new "Backend development" family even though half of it is
    exactly that.
    """
    specs = [str(s).strip() for s in specialisations if str(s).strip()]
    if not specs:
        return False
    return all(spec in SPECIALISATIONS.get(domain, []) for spec in specs)


def chain_for(spec: str) -> list[str] | None:
    """Reverse lookup: specialisation -> its chain, if it exists."""
    for domain, specs in SPECIALISATIONS.items():
        if spec in specs:
            return [domain, spec]
    return None


def shares_scope(domain_a: str, specs_a: Iterable[str], domain_b: str, specs_b: Iterable[str]) -> bool:
    """
    Do two families overlap enough that verdicts could transfer between them?

    Sharing at least one SPECIALISATION is the signal. Sharing a domain alone is
    far too loose — everything is "Programming", and a frontend verdict tells
    you nothing about a data-science ad.
    """
    if domain_a != domain_b:
        return False
    return bool(set(specs_a) & set(specs_b))


def in_scope(domain: str, specialisations: Iterable[str], ad_spec: str) -> bool:
    """
    Is an ad's specialisation inside this family's coverage?

    This is the boundary that stops "deferred can never be rejected" from
    leaking forever: an ad from a field the family does not cover is simply not
    part of the family, and is never carried into it.
    """
    return domain and ad_spec in set(specialisations)