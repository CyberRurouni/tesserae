"""
Core — the nation's resources.

Every raw or packaged resource lives under this package and is re-exported
here, so each city (modules/*) reaches for resources with a single import:

    from core import AdRecord, safe_redis_operation, AIClient

Cities never import each other's internals; they draw from the core.
"""

# ---------
# Genesis
# ---------

from genesis.config import (
    gemini_client,
    GEMINI_MODEL,
    REDIS_HOST,
    REDIS_PORT,
    DATA_DIR,
    ADS_DIR,
    KEYWORDS_DIR,
    COVERAGE_DIR,
    EXPORTS_DIR,
    BROWSER_PROFILE_DIR,
    compute_profile_hashes,
    PROFILE_HASH_TTL,
)

### ==========
### Core
### ==========

# ---------- Utilities ----------
from .utils.redis_utils import verdict_broker, run_state_broker, keyword_broker
from .utils.redis_utils import RedisStreamHandler
from .utils.redis_utils import safe_redis_operation

from .utils.ai_utils import (
    AIClient,
    JsonStreamReader,
    extract_balanced_json,
    looks_truncated,
    ApiErrorHandler,
    is_retryable,
)

# ---------- Schemas ----------
from .schemas import (
    AdRecord,
    AdRelevanceVerdict,
    CATEGORIES,
    DEFAULT_CATEGORY_ID,
    Category,
    CategoryHeuristics,
    DeliveryStatus,
    MediaType,
    Platform,
    ScrapingFilters,
    DatePreset,
    SortOrder,
    KeywordStatus,
    KeywordStats,
    KeywordGenerationRequest,
    KeywordBatch,
    KeywordTimingRequest,
    KeywordTiming,
    KeywordTimingResult,
    KeywordDisposition,
    PromptTimingRequest,
    PromptTiming,
    PoolReviewRequest,
    PoolReviewResult,
    ExclusionPrompt,
    UserProfile,
    TimeWindow,
    RunConfig,
    ChangeKind,
    Family,
    FamilyClassification,
    RequestClassification,
    RequestFamily,
    ScopeCheck,
)

# ----------
# Interface
# ----------
