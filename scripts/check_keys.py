"""One minimal call per API key → PASS/FAIL. Never prints key values.

Run from the repo root:  .venv/bin/python scripts/check_keys.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.config import ROOT, env, env_list, redact  # noqa: E402
from backend.core.llm import call_gemini  # noqa: E402

HELLO_WAV = ROOT / "demo" / "test_audio" / "hello_hi.wav"
FIRECRAWL_URL = "https://www.incometax.gov.in/iec/foportal/"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {redact(detail)}")


def require(var: str) -> bool:
    if not env(var):
        record(var, False, "not set in .env")
        return False
    return True


def check_gemini() -> None:
    # (key, primary model, use fallback chain)
    plans = [
        ("GEMINI_API_KEY_DEMO", env("GEMINI_TEXT_MODEL"), True),  # text model, then fallbacks
        ("GEMINI_API_KEY_BACKUP", None, True),  # fallback chain only
        ("GEMINI_API_KEY_DATA", env("GEMINI_DATA_MODEL"), False),
    ]
    for var, model, fallback in plans:
        if not require(var):
            continue
        tried = model or ",".join(env_list("GEMINI_FALLBACK_MODELS")) or "(none configured)"
        try:
            res = call_gemini("Reply with the single word OK.", model=model, key=var, fallback=fallback)
            note = f" (primary {model} failed → fallback)" if model and res.model != model else ""
            record(var, bool(res.text),
                   f"model={res.model}{note} attempts={res.attempts} reply={res.text.strip()[:20]!r}")
        except Exception as e:  # noqa: BLE001 - report any failure as FAIL
            record(var, False, f"tried={tried} {type(e).__name__}: {e}")


def check_sarvam() -> None:
    from sarvamai import SarvamAI
    from sarvamai.play import save

    if not require("SARVAM_API_KEY"):
        return
    try:
        client = SarvamAI(api_subscription_key=env("SARVAM_API_KEY"))
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


def firecrawl_remaining_credits() -> int | str:
    r = httpx.get("https://api.firecrawl.dev/v1/team/credit-usage",
                  headers={"Authorization": f"Bearer {env('FIRECRAWL_API_KEY')}"}, timeout=20)
    r.raise_for_status()
    data = r.json().get("data", {})
    return data.get("remaining_credits", data.get("remainingCredits", "n/a"))


def check_firecrawl() -> None:
    from firecrawl import Firecrawl

    if not require("FIRECRAWL_API_KEY"):
        return
    try:
        fc = Firecrawl(api_key=env("FIRECRAWL_API_KEY"))
        doc = fc.scrape(FIRECRAWL_URL, formats=["markdown"], only_main_content=True, wait_for=3000)
        md = (doc.markdown or "").strip()
        credits = doc.metadata.credits_used if doc.metadata else None
        try:
            remaining = firecrawl_remaining_credits()
        except Exception as e:  # noqa: BLE001 - balance is nice-to-have
            remaining = f"n/a ({type(e).__name__})"
        print("---- first 300 chars of markdown ----")
        print(md[:300])
        print("-------------------------------------")
        record("FIRECRAWL_API_KEY", bool(md),
               f"{FIRECRAWL_URL} {len(md)} chars · credits_used={credits} · remaining_credits={remaining}")
    except Exception as e:  # noqa: BLE001
        record("FIRECRAWL_API_KEY", False, f"{type(e).__name__}: {e}")


def main() -> int:
    if not (ROOT / ".env").exists():
        print("No .env found — copy .env.example to .env and fill in the keys.")
        return 1
    check_gemini()
    check_sarvam()
    check_firecrawl()
    print("[INFO] BHASHINI_API_KEY:", "set (not tested yet)" if env("BHASHINI_API_KEY") else "not set (optional)")

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f" · failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
