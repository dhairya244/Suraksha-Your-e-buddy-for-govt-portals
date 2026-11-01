"""Sarvam speech (REST): Bulbul v3 TTS → WAV, Saaras v3 STT ← WAV (PLAYBOOK §12, simplified for M3).

The desktop pill plays `say.audio_b64` as WAV and sends hold-to-ask audio as 16 kHz mono WAV,
so REST is enough here; streaming can replace it behind the same two functions.
Audio and text are never logged or written to disk.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass

from sarvamai import SarvamAI

from backend.config import env, env_float, redact

log = logging.getLogger("suraksha.voice")

# Bulbul v3 languages
LANG_CODE = {"hi": "hi-IN", "en": "en-IN", "gu": "gu-IN", "mr": "mr-IN", "bn": "bn-IN", "ta": "ta-IN", "te": "te-IN",
             "kn": "kn-IN", "ml": "ml-IN", "pa": "pa-IN", "od": "od-IN"}
CODE_LANG = {v: k for k, v in LANG_CODE.items()}
TTS_MODEL = "bulbul:v3"
TTS_SPEAKER = env("SARVAM_SPEAKER") or "kavya"  # one consistent, soft voice for every language (.env SARVAM_SPEAKER)
TTS_PACE = env_float("SARVAM_PACE", 0.9)

# Hinglish typed/transcribed in Latin script → Hindi
_HINGLISH = re.compile(r"\b(hai|hain|hoon|hu|kya|kaise|kaun|kaunsa|kahan|kyun|mujhe|mera|meri|mere|aap|apna|karna|karu|karun|"
                       r"karo|banana|banwana|chahiye|nahi|nahin|haan|iske|uske|liye|batao|bataiye|wala|wali|kab|kitna|aur|bhi|ka|ki|ke)\b", re.I)
_SCRIPTS = [("hi", r"[\u0900-\u097F]"), ("gu", r"[\u0A80-\u0AFF]"), ("bn", r"[\u0980-\u09FF]"), ("pa", r"[\u0A00-\u0A7F]"),
            ("od", r"[\u0B00-\u0B7F]"), ("ta", r"[\u0B80-\u0BFF]"), ("te", r"[\u0C00-\u0C7F]"), ("kn", r"[\u0C80-\u0CFF]"),
            ("ml", r"[\u0D00-\u0D7F]")]


def detect_lang(text: str, stt_code: str | None = None) -> str:
    """Reply language for one utterance: Latin Hinglish → hi; otherwise the script, else STT's guess, else en."""
    counts = {lang: len(re.findall(rx, text)) for lang, rx in _SCRIPTS}
    best = max(counts, key=counts.get)
    if counts[best] >= 2:
        return "mr" if best == "hi" and stt_code == "mr-IN" else best  # Marathi shares Devanagari
    if len(_HINGLISH.findall(text)) >= 1 and re.search(r"[a-z]", text, re.I):
        return "hi"
    if stt_code in CODE_LANG:
        return CODE_LANG[stt_code]
    return "en"
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
        speaker=TTS_SPEAKER, pace=TTS_PACE,
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
    return Heard(text=text, lang=detect_lang(text, code)) if text else None


async def stt(wav: bytes, lang_hint: str | None) -> Heard | None:
    try:
        return await asyncio.to_thread(stt_sync, wav, lang_hint)
    except Exception as e:  # noqa: BLE001
        log.warning("stt failed: %s", redact(f"{type(e).__name__}: {e}")[:200])
        return None
