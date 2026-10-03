"""
Genesis — Tesserae's border to the outside world.

Everything that touches external systems lives here and only here:
the Gemini SDK, environment variables, and the filesystem/Redis/browser
locations the rest of the country consumes as plain values.

Nothing else in Tesserae imports `os`, `dotenv`, or `google.genai`.
"""

import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv
from google import genai

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# ============================================================================
# 🔹 GEMINI (the AI checkpoint engine)
# ============================================================================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# `None` when no key is configured — AIClient raises a clear RuntimeError
# the moment a checkpoint actually needs it, not at import time.
gemini_client: genai.Client | None = (
    genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
)

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

# ============================================================================
# 🔹 REDIS (keyword cooldowns, dedup store, run state)
# ============================================================================

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

# ============================================================================
# 🔹 STORAGE (JSON files — the primary store, no database)
# ============================================================================

DATA_DIR = BASE_DIR / "data"
ADS_DIR = DATA_DIR / "ads"                # fetched ads, per category
KEYWORDS_DIR = DATA_DIR / "keywords"      # keyword history / batches
COVERAGE_DIR = DATA_DIR / "coverage"      # persisted exclusion prompts
EXPORTS_DIR = BASE_DIR / "exports"        # per-run Excel exports

# User-supplied profile texts (paste/edit these files directly).
PROFILE_DIR = DATA_DIR / "profile"
ABOUT_US_PATH = PROFILE_DIR / "about_us.txt"
ADDITIONAL_FILTERS_PATH = PROFILE_DIR / "additional_filters.txt"

# ============================================================================
# 🔹 BROWSER (persistent Playwright profile — log in to Google once)
# ============================================================================

BROWSER_PROFILE_DIR = BASE_DIR / "browser_profile"

# Ensure the storage tree exists on first import.
for _dir in (ADS_DIR, KEYWORDS_DIR, COVERAGE_DIR, EXPORTS_DIR, BROWSER_PROFILE_DIR, PROFILE_DIR):
    _dir.mkdir(parents=True, exist_ok=True)


def read_profile_texts() -> tuple[str, str]:
    """
    Read the user's about-us text and additional filters from the profile
    files (missing files -> empty strings; edit data/profile/*.txt to
    change what the AI sees).
    """
    about = ABOUT_US_PATH.read_text(encoding="utf-8").strip() if ABOUT_US_PATH.exists() else ""
    filters = ADDITIONAL_FILTERS_PATH.read_text(encoding="utf-8").strip() if ADDITIONAL_FILTERS_PATH.exists() else ""
    return about, filters


# ============================================================================
# 🔹 PROFILE HASHES — for profile-aware dedup keys
# ============================================================================

PROFILE_HASH_TTL = 90 * 86400  # 90 days in seconds

def compute_profile_hashes() -> tuple[str, str]:
    """
    Compute stable hashes of the user's profile texts.

    Returns (about_hash, filter_hash) — each 16-char hex (sha256[:16]).
    Empty files -> hash of empty string (deterministic).
    """
    about = ABOUT_US_PATH.read_text(encoding="utf-8").strip() if ABOUT_US_PATH.exists() else ""
    filters = ADDITIONAL_FILTERS_PATH.read_text(encoding="utf-8").strip() if ADDITIONAL_FILTERS_PATH.exists() else ""
    about_hash = hashlib.sha256(about.encode("utf-8")).hexdigest()[:16]
    filter_hash = hashlib.sha256(filters.encode("utf-8")).hexdigest()[:16]
    return about_hash, filter_hash
