"""
fingerprint.py — identity for an ad that does not depend on its Library ID.

Meta issues a SEPARATE Library ID per platform. The same advertiser running
one creative across Facebook, Instagram, Messenger, Audience Network,
WhatsApp and Threads produces five ads that are identical in every respect a
person would care about — and five ids.

Id-based dedup, which is all the scraper had, cannot see that. Observed in one
run: five accepted rows, five distinct `ad_archive_id` values, byte-identical
ad text, same advertiser, same start date. Every one was a genuine lead the
user had already seen.

So identity here is the CONTENT: a hash of the advertiser plus the ad text,
whitespace-normalised. That collapses the five rows to one while leaving
genuinely different ads alone — two advertisers quoting the same boilerplate
differ by advertiser, and one advertiser running two different creatives
differs by text.

Deliberately excluded from the fingerprint:
  * the Library ID  — that is the thing being collapsed
  * platforms       — the source of the duplication
  * dates, labels   — a relaunch of the same creative is the same lead
  * the search keyword — one ad matching three keywords is one ad

A near-duplicate that differs only by a tracking parameter or an emoji will not
be caught by an exact hash. That is the deliberate trade: a fuzzy match
wrongly merges two real leads, and a duplicate that survives costs the user one
extra row they can delete from the Decisions page. Both are recoverable; only
one of them is silent.
"""

import hashlib
import re

# Collapse whitespace runs and strip the characters Meta injects around
# creatives. Case is folded because creative casing is not meaningful and
# changes between platform renderings of the same ad.
_WHITESPACE = re.compile(r"\s+")
_VARIATION = re.compile(r"[-‏‪-‮﻿]")  # zero-width and bidi marks


def normalize_ad_text(text: str | None) -> str:
    """The comparable form of an ad's body text."""
    if not text:
        return ""
    cleaned = _VARIATION.sub("", text)
    return _WHITESPACE.sub(" ", cleaned).strip().lower()


def ad_fingerprint(advertiser: str | None, ad_text: str | None) -> str:
    """
    Stable identity for the creative an ad is showing.

    Returns "" for an ad with no text, because without text there is nothing to
    compare and an empty hash would collapse every textless ad into one.
    """
    body = normalize_ad_text(ad_text)
    if not body:
        return ""
    who = (advertiser or "").strip().lower()
    digest = hashlib.sha1(f"{who}\x00{body}".encode("utf-8")).hexdigest()
    return digest[:16]
