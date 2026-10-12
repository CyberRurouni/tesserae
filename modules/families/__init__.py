"""
families/ — profiles, their requests, and their lead lists.

Kept after the family model was simplified: profiles still exist (one job
objective each, with their own per-profile rejections and lead list), but they
no longer carry a taxonomy, lineage, or seeding. Creating a profile now just
means starting with empty per-profile state — accepted leads and universal junk
are global and carry over on their own.

  store      JSON-first persistence for profiles, requests and per-profile leads
  classifier judges whether an edit to a profile or request is cosmetic or
             structural. Classification and application stay separate so a
             declined change writes nothing.
  deferred   the "not now" bucket — full ad records, TTL renewed on re-encounter
  active     which profile is current, and bootstrap from the profile files
"""

from . import active, deferred, store
from .classifier import classify_profile_change, classify_request_change

__all__ = [
    "store",
    "active",
    "deferred",
    "classify_profile_change",
    "classify_request_change",
]
