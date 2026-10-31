"""One minimal call per API key → PASS/FAIL. Never prints key values.

Run from the repo root:  .venv/bin/python scripts/check_keys.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

GEMINI_KEYS = ["GEMINI_API_KEY_DEMO", "GEMINI_API_KEY_BACKUP", "GEMINI_API_KEY_DATA"]
SECRET_VARS = GEMINI_KEYS + ["SARVAM_API_KEY", "FIRECRAWL_API_KEY", "BHASHINI_API_KEY"]
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
HELLO_WAV = ROOT / "demo" / "test_audio" / "hello_hi.wav"
FIRECRAWL_URL = "https://www.digitalgujarat.gov.in/"

results: list[tuple[str, bool, str]] = []


def redact(text: str) -> str:
    """Strip any key value that an SDK might echo back in an error message."""
    for var in SECRET_VARS:
        value = os.getenv(var)
        if value and len(value) > 4:
            text = text.replace(value, f"<{var}>")
    return text


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {redact(detail)}")


def require(var: str) -> str | None:
    value = os.getenv(var, "").strip()
    if not value:
        record(var, False, "not set in .env")
        return None
    return value


def check_gemini() -> None:
    from google import genai

    model = os.getenv("GEMINI_TEXT_MODEL", "").strip() or DEFAULT_GEMINI_MODEL
    for var in GEMINI_KEYS:
        key = require(var)
        if not key:
            continue
        try:
            client = genai.Client(api_key=key)
            resp = client.models.generate_content(model=model, contents="Reply with the single word OK.")
            record(var, bool(resp.text), f"model={model} reply={resp.text.strip()[:20]!r}")
        except Exception as e:  # noqa: BLE001 - report any failure as FAIL
            record(var, False, f"model={model} {type(e).__name__}: {e}")


def check_sarvam() -> None:
    from sarvamai import SarvamAI
    from sarvamai.play import save

    key = require("SARVAM_API_KEY")
    if not key:
        return
    try:
        client = SarvamAI(api_subscription_key=key)
        audio = client.text_to_speech.convert(
            text="नमस्ते, मैं सुरक्षा हूँ। मैं आपका फ़ॉर्म भरने में मदद करूँगी।",
            language_code="hi-IN",
            model="bulbul:v3",
            speaker="priya",
        )
        HELLO_WAV.parent.mkdir(parents=True, exist_ok=True)
        save(audio, str(HELLO_WAV))
        size = HELLO_WAV.stat().st_size
        record("SARVAM_API_KEY", size > 1000, f"bulbul:v3 hi-IN → {HELLO_WAV.relative_to(ROOT)} ({size} bytes)")
    except Exception as e:  # noqa: BLE001
        record("SARVAM_API_KEY", False, f"{type(e).__name__}: {e}")


def check_firecrawl() -> None:
    from firecrawl import Firecrawl

    key = require("FIRECRAWL_API_KEY")
    if not key:
        return
    try:
        fc = Firecrawl(api_key=key)
        doc = fc.scrape(FIRECRAWL_URL, formats=["markdown"], only_main_content=True)
        md = (doc.markdown or "").strip()
        credits = doc.metadata.credits_used if doc.metadata else None
        try:
            remaining = fc.get_credit_usage().remaining_credits
        except Exception:  # noqa: BLE001 - balance is nice-to-have
            remaining = "n/a"
        print("---- first 300 chars of markdown ----")
        print(md[:300])
        print("-------------------------------------")
        record("FIRECRAWL_API_KEY", bool(md),
               f"{len(md)} chars · credits_used={credits} · remaining_credits={remaining}")
    except Exception as e:  # noqa: BLE001
        record("FIRECRAWL_API_KEY", False, f"{type(e).__name__}: {e}")


def main() -> int:
    if not (ROOT / ".env").exists():
        print("No .env found — copy .env.example to .env and fill in the keys.")
        return 1
    check_gemini()
    check_sarvam()
    check_firecrawl()
    print("[INFO] BHASHINI_API_KEY:", "set (not tested yet)" if os.getenv("BHASHINI_API_KEY") else "not set (optional)")

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f" · failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
