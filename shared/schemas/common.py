"""Shared building blocks: sourced facts, verification status, government level."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator

UNKNOWN = "unknown"


class StrictModel(BaseModel):
    """Base for all data contracts: unknown keys are an error, not silently dropped."""

    model_config = ConfigDict(extra="forbid")


class VerificationStatus(str, Enum):
    verified = "verified"
    partially_verified = "partially_verified"
    pending = "pending"
    rejected = "rejected"


class GovernmentLevel(str, Enum):
    central = "central"
    state = "state"
    district = "district"
    local = "local"


class Fact(StrictModel):
    """One factual claim, traceable to the exact text on an official page.

    `value` may be the literal "unknown" when no official source states it;
    otherwise `quote` and `source_url` are required.
    """

    value: str
    quote: str | None = None
    source_url: str | None = None

    @model_validator(mode="after")
    def _known_facts_need_a_source(self) -> "Fact":
        if self.value != UNKNOWN and not (self.quote and self.source_url):
            raise ValueError("a known Fact needs both quote and source_url (else value='unknown')")
        return self

    @classmethod
    def unknown(cls) -> "Fact":
        return cls(value=UNKNOWN)

    @property
    def is_unknown(self) -> bool:
        return self.value == UNKNOWN
