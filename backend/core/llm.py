"""Gemini calls with retry/backoff and model fallback. Shared by the backend and scripts/.

    from backend.core.llm import call_gemini
    res = call_gemini(["Say OK"], model=env("GEMINI_TEXT_MODEL"), key="GEMINI_API_KEY_DEMO")
    res.text, res.parsed, res.model

`key` is the *name* of the env var holding the API key, so raw keys never travel through
call sites or logs. Logs record model, attempts and latency only — never prompt content.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from backend.config import env_float, env_int, env_list, env_required, redact

log = logging.getLogger("suraksha.llm")

RETRYABLE_CODES = {429, 500, 503, 504}
FALLBACK_CODES = RETRYABLE_CODES | {404}  # 404 = model id not available on this key
# per-model tries and request timeout; the live app wants fail-fast (one try, short timeout)
MAX_TRIES = env_int("GEMINI_MAX_TRIES", 1)
REQUEST_TIMEOUT_S = env_float("GEMINI_TIMEOUT_SECONDS", 8.0)

_clients: dict[tuple[str, float], genai.Client] = {}
# (model, key) → monotonic time until which a 429'd model is skipped (if another model is left to try)
_cooldown_until: dict[tuple[str, str], float] = {}
DEFAULT_429_COOLDOWN_S = 60.0


@dataclass
class LLMResult:
    text: str
    model: str
    parsed: Any = None  # schema instance when `schema` was given
    attempts: int = 1


class LLMError(RuntimeError):
    pass


def get_client(key: str, timeout_s: float = REQUEST_TIMEOUT_S) -> genai.Client:
    """One genai.Client per (key, timeout), reused across calls."""
    cache_key = (key, timeout_s)
    if cache_key not in _clients:
        _clients[cache_key] = genai.Client(
            api_key=env_required(key),
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
        )
    return _clients[cache_key]


def _retry_delay_s(err: errors.APIError) -> float | None:
    """Server-suggested delay from a 429 RetryInfo ("retryDelay": "17s"), if any."""
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', json.dumps(err.details or {}))
    return float(m.group(1)) if m else None


def cooling(model: str, key: str) -> bool:
    """True while (model, key) is in a 429 cooldown."""
    return _cooldown_until.get((model, key), 0) > time.monotonic()


def call_gemini(
    parts: list[Any] | str,
    model: str | None,
    key: str,
    schema: type[BaseModel] | Any | None = None,
    *,
    fallback: bool = True,
    temperature: float = 0.1,
    timeout_s: float = REQUEST_TIMEOUT_S,
    max_backoff_s: float = 10.0,
    max_tries: int | None = None,
    system_instruction: str | None = None,
    thinking_level: str | None = None,
    backup_key: str | None = None,
) -> LLMResult:
    """Call `model` (up to `max_tries`, default GEMINI_MAX_TRIES, on 429/5xx/timeout), then each GEMINI_FALLBACK_MODELS entry.

    `backup_key`: env var name of a second key, tried once (same models) if every model failed on `key`.
    `thinking_level` ("minimal" | "low" | ...) caps Gemini 3 thinking; vision uses "low" (~7 s → ~2.5 s).
    `schema` may be a pydantic model class (parsed into `.parsed`) or any type the SDK accepts
    as response_schema (e.g. list[Model]).
    """
    models = [m for m in [model] if m]
    if fallback:
        models += [m for m in env_list("GEMINI_FALLBACK_MODELS") if m not in models]
    if not models:
        raise LLMError("no model given and GEMINI_FALLBACK_MODELS is empty")

    client = get_client(key, timeout_s)
    config = types.GenerateContentConfig(temperature=temperature, system_instruction=system_instruction)
    if thinking_level:
        config.thinking_config = types.ThinkingConfig(thinking_level=thinking_level)
    if schema is not None:
        config.response_mime_type = "application/json"
        config.response_schema = schema

    now = time.monotonic()
    ready = [m for m in models if _cooldown_until.get((m, key), 0) <= now]
    models = ready or models  # never skip everything

    tries = max_tries or MAX_TRIES
    last_err: str = ""
    total_attempts = 0
    for m in models:
        for attempt in range(1, tries + 1):
            total_attempts += 1
            t0 = time.monotonic()
            try:
                resp = client.models.generate_content(model=m, contents=parts, config=config)
                text = resp.text or ""
                parsed = None
                if schema is not None:
                    parsed = resp.parsed
                    if parsed is None and isinstance(schema, type) and issubclass(schema, BaseModel):
                        parsed = schema.model_validate_json(text)
                log.info("gemini ok model=%s key=%s attempts=%d latency_ms=%d",
                         m, key, total_attempts, (time.monotonic() - t0) * 1000)
                return LLMResult(text=text, model=m, parsed=parsed, attempts=total_attempts)
            except errors.APIError as e:
                last_err = f"{m}: HTTP {e.code} {e.status or ''}".strip()
                log.warning("gemini fail model=%s key=%s code=%s attempt=%d", m, key, e.code, attempt)
                if e.code not in FALLBACK_CODES:
                    raise LLMError(redact(f"{m}: HTTP {e.code} {e.message}")) from e
                if e.code == 404:
                    break  # model unavailable → next model, no point retrying
                if e.code == 429:
                    _cooldown_until[(m, key)] = time.monotonic() + (_retry_delay_s(e) or DEFAULT_429_COOLDOWN_S)
                    if m != models[-1] or tries == 1:
                        break  # rate-limited → go straight to the next model (or the caller's next key)
                delay = _retry_delay_s(e) or (2 ** (attempt - 1)) + random.random()
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = f"{m}: {type(e).__name__}"
                log.warning("gemini fail model=%s key=%s err=%s attempt=%d", m, key, type(e).__name__, attempt)
                delay = (2 ** (attempt - 1)) + random.random()
            if attempt < tries:
                time.sleep(min(delay, max_backoff_s))
    if backup_key and backup_key != key and env_required(backup_key):
        log.warning("gemini all models failed on key=%s, retrying on key=%s", key, backup_key)
        return call_gemini(parts, model, backup_key, schema, fallback=fallback, temperature=temperature, timeout_s=timeout_s,
                           max_backoff_s=max_backoff_s, max_tries=max_tries, system_instruction=system_instruction,
                           thinking_level=thinking_level)
    raise LLMError(redact(f"all models failed ({', '.join(models)}); last: {last_err}"))
