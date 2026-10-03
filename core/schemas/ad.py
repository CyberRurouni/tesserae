"""
AdRecord — the raw resource representing one fetched ad.

Produced by the scraper city, consumed by the AI-filtering checkpoint,
the JSON store, and the Excel exporter. Every ad in the pipeline is
validated against this shape.
"""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class AdRecord(BaseModel):
    ad_archive_id: str
    advertiser_name: str | None = None
    advertiser_id: str | None = None
    advertiser_page_url: str | None = None
    # Spend proxy: how many ads this advertiser currently has running.
    total_active_ads: int | None = None

    ad_text: str = ""
    cta_text: str | None = None
    cta_url: str | None = None

    platforms: list[str] = Field(default_factory=list)
    started_date: datetime | None = None
    end_date: datetime | None = None
    is_active: bool | None = None
    delivered_regions: list[str] = Field(default_factory=list)
    ad_age_hours: float | None = None

    # Pipeline provenance
    category: str | None = None            # the lens this ad was judged under
    source_keyword: str | None = None
    heuristic_labels: list[str] = Field(default_factory=list)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
