"""Process-level details for a service: eligibility, documents, fees, steps (every fact sourced)."""
from __future__ import annotations

from .common import Fact, StrictModel


class Document(StrictModel):
    name: str
    formats: list[str] = []  # e.g. ["pdf", "jpg"]
    max_size_kb: int | None = None
    fact: Fact  # the quote stating this document is required


class ServiceDetails(StrictModel):
    service_id: str
    eligibility: list[Fact] = []
    documents: list[Document] = []
    fees: list[Fact] = []
    timeline: Fact = Fact.unknown()
    issuing_authority: Fact = Fact.unknown()
    steps: list[Fact] = []
    tracking_url: str | None = None
    appeal: Fact = Fact.unknown()
    disclaimer: str | None = None  # e.g. "Guidance only, not tax advice." for ITR
