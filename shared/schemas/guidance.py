"""Pre-generated field guidance (PLAYBOOK §7.5) → data/guidance/<process_id>/<lang>.json."""
from __future__ import annotations

from pydantic import RootModel

from .common import StrictModel


class GuidanceEntry(StrictModel):
    say: str
    sources: list[str] = []
    reviewed: bool = False


class GuidanceFile(RootModel[dict[str, GuidanceEntry]]):
    """Keyed by field_id."""
