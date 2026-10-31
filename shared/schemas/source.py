"""Official sources that facts are collected from (data/raw/<...>/urls.jsonl, meta.jsonl)."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from .common import GovernmentLevel, StrictModel


class SourceType(str, Enum):
    portal = "portal"
    directory = "directory"
    pdf = "pdf"
    faq = "faq"
    dataset = "dataset"
    api = "api"


class CollectionMethod(str, Enum):
    scrape = "scrape"
    manual = "manual"
    download = "download"
    api = "api"


class Source(StrictModel):
    source_id: str
    name: str
    official_url: str
    government_level: GovernmentLevel
    state: str | None = None
    department: str | None = None
    source_type: SourceType
    collection_method: CollectionMethod
    last_checked_at: datetime
