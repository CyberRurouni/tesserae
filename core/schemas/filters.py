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
    # An explicit date range, as Meta's own "Impressions by date" From/To.
    # Either side may be omitted for an open-ended range. When set this REPLACES
    # ad_age_hours, which is the same idea expressed relatively ("last 7 days").
    #
    # There used to be a `date_preset` here offering any/last_7_days/last_30_days
    # plus a `custom` option. It never reached Meta, its only non-redundant
    # option was not in the UI at all, and every option it did offer duplicated
    # ad_age_hours — so the Search page showed two identical date dropdowns, the
    # first of which silently did nothing. Removed rather than left lying.
    custom_start_date: date | None = None
    custom_end_date: date | None = None
    delivered_to_regions: list[str] = Field(default_factory=list)  # ISO codes, e.g. ["PK"]
    languages: list[str] = Field(default_factory=list)
    sort_order: SortOrder = SortOrder.relevance

    # --- Tesserae extras (not offered by the Meta UI) ---
    ad_age_hours: int | None = None        # e.g. 168 = less than a week old

    # Heuristic backbone: ad text must contain every required pattern (AND),
    # at least one of the any-patterns (OR), and must NOT contain any excluded
    # pattern — before the AI checkpoint ever sees it. Typically sourced
    # from the selected category's definition.
    require_ad_text_patterns: list[str] = Field(default_factory=list)      # AND
    require_any_ad_text_patterns: list[str] = Field(default_factory=list)  # OR
    exclude_ad_text_patterns: list[str] = Field(default_factory=list)
