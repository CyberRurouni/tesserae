"""
profile.py — read the user's paste-ready profile texts.

The files live at data/profile/ (genesis owns the paths). The orchestrator
feeds about-us to Actors 1 & 3; checkpoint (a) appends additional filters
to its judging prompt.
"""

import logging

from genesis.config import read_profile_texts

logger = logging.getLogger(__name__)


def load_profile() -> tuple[str, str]:
    """(about_us, additional_filters) — empty strings when files are missing."""
    about, extra = read_profile_texts()
    if not about:
        logger.warning("⚠️ data/profile/about_us.txt is empty or missing — AI actors lose context")
    return about, extra
