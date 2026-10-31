"""Official terms in each demo language (PLAYBOOK §7.4) → data/glossary.json."""
from __future__ import annotations

from pydantic import RootModel

from .common import StrictModel


class GlossaryEntry(StrictModel):
    term: str  # English / official term
    hi: str | None = None
    gu: str | None = None  # copied verbatim from the portal, never machine-translated
    keep_english: bool = False  # e.g. "Aadhaar", "OTP", "PIN code"
    source_url: str | None = None  # page the Gujarati/Hindi term was copied from


class Glossary(RootModel[list[GlossaryEntry]]):
    pass
