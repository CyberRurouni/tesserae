"""
Genesis — Tesserae's border to the outside world.

Everything that touches external systems lives here and only here:
the Gemini SDK, environment variables, and the filesystem/Redis/browser
locations the rest of the country consumes as plain values.

Nothing else in Tesserae imports `os`, `dotenv`, or `google.genai`.
"""

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

# Overridable so tests — and anyone running a second instance — never have to
# write into the real data directory. Everything below derives from this.
DATA_DIR = Path(os.getenv("TESSERAE_DATA_DIR") or (BASE_DIR / "data")).resolve()
ADS_DIR = DATA_DIR / "ads"                # fetched ads, per category
KEYWORDS_DIR = DATA_DIR / "keywords"      # keyword history / batches
COVERAGE_DIR = DATA_DIR / "coverage"      # persisted exclusion prompts
EXPORTS_DIR = BASE_DIR / "exports"        # per-run Excel exports
PROFILES_DIR = DATA_DIR / "profiles"       # profile + request records
LEADS_DIR = DATA_DIR / "leads"            # per-profile accepted ads (never pruned)

# Profiles are NOT stored here. They are entities with ids, held in
# DATA_DIR/profiles/profiles.json (modules.profiles.store) alongside the active
# pointer. This used to be a pair of text files — about_us.txt and
# additional_filters.txt — holding the "currently active" profile, which every
# run then read. Those files were the original storage, superseded when profiles
# became real objects, and they outlived their usefulness as a mirror that three
# separate code paths had to keep in sync. Worse, the mirror was authoritative:
# it overwrote the store at the start of every run, so a stale file silently
# corrupted the profiles. One store, one writer.

# ============================================================================
# 🔹 BROWSER (persistent Playwright profile — log in to Google once)
# ============================================================================

BROWSER_PROFILE_DIR = BASE_DIR / "browser_profile"

# Ensure the storage tree exists on first import.
for _dir in (ADS_DIR, KEYWORDS_DIR, COVERAGE_DIR, EXPORTS_DIR, BROWSER_PROFILE_DIR,
             PROFILES_DIR, LEADS_DIR):
    _dir.mkdir(parents=True, exist_ok=True)
