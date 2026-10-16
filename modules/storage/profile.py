"""
profile.py — the texts the AI actors judge against.

Sourced from the profile STORE, for the profile a run is actually under — not
from "whatever the most recent active profile happened to be". This is the
single lookup both the keyword generator and the relevance judge go through,
which is the only reason they cannot end up reading two different people: they
are handed one resolved object.

Returns empty strings when there is no profile at all rather than raising,
because a run started before the About You page has been filled in should
still be able to happen — it will just judge against nothing, and say so.
"""

import logging

from modules.profiles import active as active_module
from modules.profiles import store

logger = logging.getLogger(__name__)


def resolve_lens(profile_id: str | None = None, request_id: str | None = None) -> tuple[str, str]:
    """
    The profile text and request text for one run.

    An explicitly named profile wins over the active pointer: the caller said
    which one it means, and second-guessing that is how a run ends up judged
    against somebody else's profile. Falling back to the active pointer is for
    callers that have no opinion — the CLI, and a direct API call that omitted
    the field.
    """
    profile = None
    if profile_id:
        profile = store.get_profile(profile_id)
        if profile is None:
            logger.warning("⚠️ Profile %r not found — falling back to the active one", profile_id)
    if profile is None:
        profile = active_module.active_profile()

    if profile is None:
        logger.warning("⚠️ No profile exists — the AI actors will have no context to judge against")
        return "", ""

    text = profile.profile_text or ""
    if not text.strip():
        logger.warning(
            "⚠️ Profile %r has empty text — write it on the 'About you' page, or every "
            "ad will be judged against nothing",
            profile.profile_id,
        )

    wanted = request_id
    if not wanted:
        # Ask THIS profile which of its own requests to use. Consulting the
        # global pointer instead would name the active profile's request, which
        # belongs to somebody else whenever the run is under a different one.
        wanted = active_module.request_for_profile(profile.profile_id)
    request_text = ""
    if wanted:
        request = store.get_request(wanted)
        # A request id from a DIFFERENT profile would scope this run's
        # accept-vs-defer decisions to someone else's wishes.
        if request and request.profile_id == profile.profile_id:
            request_text = request.request_text or ""
        elif request:
            logger.warning(
                "⚠️ Request %r belongs to profile %r, not %r — ignoring it",
                wanted, request.profile_id, profile.profile_id,
            )

    return text, request_text


def load_profile() -> tuple[str, str]:
    """(profile_text, request_text) for the active profile. See `resolve_lens`."""
    return resolve_lens()