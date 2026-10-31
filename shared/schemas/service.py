"""Directory entry for one government service (replaces PLAYBOOK §7.1 directory.json entries)."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from .common import GovernmentLevel, StrictModel, VerificationStatus


class ApplicationMode(str, Enum):
    online = "online"
    offline = "offline"
    both = "both"
    unknown = "unknown"


class Service(StrictModel):
    service_id: str
    name: str
    alternate_names: list[str] = []  # incl. Hindi / Gujarati names and romanised forms
    department: str
    government_level: GovernmentLevel
    state: str | None = None
    category: str
    application_mode: ApplicationMode
    official_url: str
    portal_id: str | None = None
    verification_status: VerificationStatus = VerificationStatus.pending
    source_ids: list[str] = []
    last_verified_at: datetime | None = None
