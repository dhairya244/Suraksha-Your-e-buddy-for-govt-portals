"""Government scheme for consultancy suggestions (PLAYBOOK §7.3) → data/schemes.json."""
from __future__ import annotations

from datetime import date
from typing import Literal

from .common import StrictModel, VerificationStatus

# Tag vocabulary from PLAYBOOK §13.5
SchemeTag = Literal[
    "farmer", "student", "women", "senior_citizen", "disability", "sc_st", "obc", "bpl",
    "urban", "rural", "business", "health", "housing", "education", "income_support",
]


class Scheme(StrictModel):
    scheme_id: str
    name: str
    level: Literal["central", "state"]
    state: str | None = None
    summary: str
    eligibility: list[str] = []
    exclusions: list[str] = []
    benefits: str | None = None
    documents: list[str] = []
    how_to_apply: str | None = None
    official_url: str
    source_url: str
    verified_on: date | None = None
    tags: list[SchemeTag] = []
    required_service_ids: list[str] = []  # e.g. ["gj_income_certificate"]
    verification_status: VerificationStatus = VerificationStatus.pending
