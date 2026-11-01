"""Sarvam speech (REST): Bulbul v3 TTS → WAV, Saaras v3 STT ← WAV (PLAYBOOK §12, simplified for M3).

The desktop pill plays `say.audio_b64` as WAV and sends hold-to-ask audio as 16 kHz mono WAV,
so REST is enough here; streaming can replace it behind the same two functions.
Audio and text are never logged or written to disk.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass

from sarvamai import SarvamAI

from backend.config import env, redact

log = logging.getLogger("suraksha.voice")

LANG_CODE = {"hi": "hi-IN", "en": "en-IN", "gu": "gu-IN"}
CODE_LANG = {v: k for k, v in LANG_CODE.items()}
TTS_MODEL = "bulbul:v3"
TTS_SPEAKER = {"hi": "priya", "en": "priya", "gu": "priya"}  # one consistent voice
STT_MODEL = "saaras:v3"

_client: SarvamAI | None = None


def client() -> SarvamAI | None:
    global _client
    if _client is None and env("SARVAM_API_KEY"):
        _client = SarvamAI(api_subscription_key=env("SARVAM_API_KEY"))
    return _client


def tts_sync(text: str, lang: str) -> bytes | None:
    c = client()
    if not c or not text.strip():
        return None
    t0 = time.monotonic()
    res = c.text_to_speech.convert(
        text=text[:1500], language_code=LANG_CODE.get(lang, "hi-IN"), model=TTS_MODEL,
        speaker=TTS_SPEAKER.get(lang, "priya"), pace=1.0,
    )
    audios = res.audios or []
    if len(audios) > 1:
        log.warning("tts returned %d chunks; playing the first", len(audios))
    wav = base64.b64decode(audios[0]) if audios else b""
    log.info("tts ok lang=%s bytes=%d latency_ms=%d", lang, len(wav), (time.monotonic() - t0) * 1000)
    return wav or None


async def tts(text: str, lang: str) -> bytes | None:
    try:
        return await asyncio.to_thread(tts_sync, text, lang)
    except Exception as e:  # noqa: BLE001 — voice failure must never break guidance
        log.warning("tts failed: %s", redact(f"{type(e).__name__}: {e}")[:200])
        return None


@dataclass
class Heard:
    text: str
    lang: str  # "hi" | "en" | "gu" | other short code


def stt_sync(wav: bytes, lang_hint: str | None) -> Heard | None:
    c = client()
    if not c:
        return None
    t0 = time.monotonic()
    res = c.speech_to_text.transcribe(
        file=("ask.wav", wav, "audio/wav"), model=STT_MODEL, mode="codemix",
        language_code="unknown",  # auto-detect so the user can switch language by speaking
    )
    text = (res.transcript or "").strip()
    code = getattr(res, "language_code", None) or LANG_CODE.get(lang_hint or "hi", "hi-IN")
    log.info("stt ok lang=%s chars=%d latency_ms=%d", code, len(text), (time.monotonic() - t0) * 1000)
    return Heard(text=text, lang=CODE_LANG.get(code, lang_hint or "hi")) if text else None


async def stt(wav: bytes, lang_hint: str | None) -> Heard | None:
    try:
        return await asyncio.to_thread(stt_sync, wav, lang_hint)
    except Exception as e:  # noqa: BLE001
        log.warning("stt failed: %s", redact(f"{type(e).__name__}: {e}")[:200])
        return None
