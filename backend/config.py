"""All configuration comes from .env (repo root). Never log key values."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

GEMINI_KEY_VARS = ("GEMINI_API_KEY_DEMO", "GEMINI_API_KEY_BACKUP", "GEMINI_API_KEY_DATA")
SECRET_VARS = GEMINI_KEY_VARS + ("SARVAM_API_KEY", "FIRECRAWL_API_KEY", "BHASHINI_API_KEY")


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, "").strip()
    return value or default


def env_required(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"{name} is not set in .env")
    return value


def env_list(name: str) -> list[str]:
    return [v.strip() for v in (env(name) or "").split(",") if v.strip()]


def env_float(name: str, default: float) -> float:
    value = env(name)
    return float(value) if value else default


def env_int(name: str, default: int) -> int:
    value = env(name)
    return int(value) if value else default


def redact(text: str) -> str:
    """Replace any secret value that appears in text (e.g. an SDK error message)."""
    for var in SECRET_VARS:
        value = os.getenv(var)
        if value and len(value) > 4:
            text = text.replace(value, f"<{var}>")
    return text
