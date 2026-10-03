"""
ScrapingFilters — the full filter set for a scrape.

Covers every filter the Meta Ads Library UI offers, plus Tesserae's own
extras (ad age, heuristic pattern filters) that the library doesn't provide.
"""

from datetime import date
from enum import Enum

from pydantic import BaseModel, Field

from .category import DEFAULT_CATEGORY_ID


class DeliveryStatus(str, Enum):
    all = "all"
    active = "active"
    inactive = "inactive"


class Platform(str, Enum):
    facebook = "facebook"
    instagram = "instagram"
    messenger = "messenger"
    audience_network = "audience_network"
    whatsapp = "whatsapp"
    threads = "threads"


class MediaType(str, Enum):
    all = "all"
    image = "image"
    video = "video"
    meme = "meme"


class DatePreset(str, Enum):
    any = "any"
    last_7_days = "last_7_days"
    last_30_days = "last_30_days"
    custom = "custom"


class SortOrder(str, Enum):
    relevance = "relevance"
    date = "date"


class ScrapingFilters(BaseModel):
    # --- Tesserae lens ---
    # The category scopes verdict keys, storage, heuristics and the AI
    # intent — one per run. Its heuristic patterns are usually supplied
    # from the category definition (Category.filter_kwargs) — the fields
    # below stay overridable for experiments.
    category: str = DEFAULT_CATEGORY_ID

    # --- Meta library filters ---
    keywords: list[str] = Field(default_factory=list)
    advertiser: str | None = None          # search within a specific page/advertiser
    advertiser_category: str | None = None
    delivery_status: DeliveryStatus = DeliveryStatus.active
    platforms: list[Platform] = Field(default_factory=list)
    media_type: MediaType = MediaType.all
    date_preset: DatePreset = DatePreset.any
    custom_start_date: date | None = None  # only used when date_preset == custom
    custom_end_date: date | None = None
    delivered_to_regions: list[str] = Field(default_factory=list)  # ISO codes, e.g. ["PK"]
    languages: list[str] = Field(default_factory=list)
    sort_order: SortOrder = SortOrder.relevance

    # --- Tesserae extras (not offered by the Meta UI) ---
    ad_age_hours: int | None = None        # e.g. 168 = less than a week old
    results_limit_per_keyword: int = 50

    # Heuristic backbone: ad text must contain every required pattern (AND),
    # at least one of the any-patterns (OR), and must NOT contain any excluded
    # pattern — before the AI checkpoint ever sees it. Typically sourced
    # from the selected category's definition.
    require_ad_text_patterns: list[str] = Field(default_factory=list)      # AND
    require_any_ad_text_patterns: list[str] = Field(default_factory=list)  # OR
    exclude_ad_text_patterns: list[str] = Field(default_factory=list)
