"""Answer a question from where the user IS: latest screenshot + session context + official data.

One multimodal Gemini call (image + text). Facts only from the context JSON (data/), with sources.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Literal

from google.genai import types
from pydantic import BaseModel, Field

from backend.config import env
from backend.core.llm import LLMError, call_gemini
from backend.knowledge import pick_context

log = logging.getLogger("suraksha.answer")

KEY = "GEMINI_API_KEY_DEMO"  # answers; vision uses GEMINI_API_KEY_BACKUP (separate quotas)
TIMEOUT_S = 12.0
LANG_NAME = {"hi": "Hindi (Devanagari script)", "en": "simple Indian English", "gu": "Gujarati (Gujarati script)"}

SYSTEM = """You are Suraksha, a voice assistant sitting next to someone who is filling a Gujarat government form
(Digital Gujarat portal) in their browser. You can see their screen (the image) and the session state.

ALWAYS START FROM WHERE THE USER IS:
- Use page_type, page title, the current field and the done/remaining fields. If they are already on the portal
  (page_type login / registration / service_list / application_form / payment), never tell them to "go to the website";
  tell them what to do on THIS screen now, then the next steps.
  e.g. on application_form, "apply kaise karu" → "You are on the form; fill <current field> now, then <remaining fields>,
  then upload documents and submit" (adapt with the official steps from the context).
- Only if page_type is 'other' (not the portal) may you tell them to open the portal.

FACTS:
- Rules, fees, documents, deadlines, eligibility and links come ONLY from the context JSON. Never invent them.
- What is visible on screen may be described from the image (labels, buttons, messages), never values the user typed.
- If the context does not contain the answer, say you are not sure, set confident=false, and point to the official page.
- Keep official terms exactly as written; do not translate official Gujarati terms.
- Never ask for or repeat Aadhaar numbers, OTPs, passwords or bank details. Never offer to fill or submit for them.
- ITR/tax: process guidance only, never tax advice.

QUESTION KINDS (set `kind`):
- page_question → answer from the screenshot + session state (what to fill/click here, in order).
- service_question → answer from the context JSON with sources.
- general / off-topic (weather, jokes, other topics) → one short polite sentence that you can only help with this
  form and government services, then offer to continue with the current field. No facts, empty sources.

STYLE: spoken aloud — plain everyday words, at most 3 short sentences (a list of up to 6 documents is fine),
no markdown, no URLs in `say`. Reply in {lang_name}; English names of portal buttons/labels are fine.
Hindi must be written in Devanagari script only (never romanized Hindi). You speak with a female voice:
use feminine first-person forms (मैं मदद कर सकती हूँ, देख रही हूँ)."""


class Answer(BaseModel):
    kind: Literal["page_question", "service_question", "general"] = Field(
        description="page_question: about what is on screen; service_question: documents/fees/steps/eligibility/timeline; general: anything else")
    say: str = Field(description="What to speak to the user")
    sources: list[str] = Field(description="source_url values from the context that support the facts in `say`")
    confident: bool


@dataclass
class ScreenContext:
    """Snapshot of the session for one answer."""
    page_type: str | None = None
    page_title: str | None = None
    logged_in: bool | None = None
    current_field: str | None = None
    fields_done: list[str] = field(default_factory=list)
    fields_remaining: list[str] = field(default_factory=list)
    ask_back: dict = field(default_factory=dict)  # e.g. {"has_digital_gujarat_account": true}
    service_id: str = "gj_income_certificate"

    def as_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], {})}


def answer_sync(question: str, lang: str, screen: ScreenContext, jpeg: bytes | None) -> Answer:
    ctx = pick_context(question, screen.service_id)
    text = (
        f"Session state: {json.dumps(screen.as_json(), ensure_ascii=False)}\n"
        f"Screenshot attached: {'yes' if jpeg else 'no (screen not captured right now)'}\n"
        f"Context JSON (official data):\n{json.dumps(ctx, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"User asked: \"{question}\""
    )
    parts: list = [types.Part.from_bytes(data=jpeg, mime_type="image/jpeg")] if jpeg else []
    parts.append(text)
    res = call_gemini(
        parts, model=env("GEMINI_TEXT_MODEL"), key=KEY, schema=Answer, temperature=0.2,
        timeout_s=TIMEOUT_S, thinking_level="low",
        system_instruction=SYSTEM.format(lang_name=LANG_NAME.get(lang, LANG_NAME["hi"])),
    )
    a: Answer = res.parsed
    log.info("answer model=%s confident=%s sources=%d image=%s page=%s", res.model, a.confident, len(a.sources), bool(jpeg), screen.page_type)
    return a


async def answer(question: str, lang: str, screen: ScreenContext | None = None, jpeg: bytes | None = None) -> Answer | None:
    try:
        return await asyncio.to_thread(answer_sync, question, lang, screen or ScreenContext(), jpeg)
    except (LLMError, ValueError) as e:
        log.warning("answer failed: %s", e)
        return None
