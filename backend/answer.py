"""Answer a spoken question from collected official data only (PLAYBOOK §13 common rules + §13.2)."""
from __future__ import annotations

import asyncio
import json
import logging

from pydantic import BaseModel, Field

from backend.config import env
from backend.core.llm import LLMError, call_gemini
from backend.knowledge import pick_context

log = logging.getLogger("suraksha.answer")

KEY = "GEMINI_API_KEY_BACKUP"  # separate quota from vision (GEMINI_API_KEY_DEMO)
LANG_NAME = {"hi": "Hindi (Devanagari script)", "en": "simple Indian English", "gu": "Gujarati (Gujarati script)"}

SYSTEM = """You are Suraksha, a voice assistant helping someone fill a Gujarat government form (Digital Gujarat portal).
Rules:
- Use ONLY the provided context JSON. Never invent rules, fees, documents, deadlines, eligibility or links.
- If the context does not contain the answer, say you are not sure, set confident=false, and point to the official page (give its URL in sources).
- Keep official terms exactly as written. Do not translate official Gujarati terms.
- Never ask for or repeat Aadhaar numbers, OTPs, passwords or bank details. Never offer to fill or submit anything for the user.
- ITR/tax: give process guidance only, never tax advice.
- Schemes: say the user "may be eligible" and must confirm on the official page.
- This is spoken aloud: plain everyday words, at most 2 short sentences (3 if listing documents), no markdown, no URLs in `say`.
- Reply in {lang_name}. English words for portal buttons/labels are fine."""


class Answer(BaseModel):
    say: str = Field(description="What to speak to the user")
    sources: list[str] = Field(description="source_url values from the context that support the answer")
    confident: bool


def answer_sync(question: str, lang: str, page_title: str | None, field_label: str | None) -> Answer:
    ctx = pick_context(question)
    prompt = (
        f"Screen: page={page_title or 'unknown'}; current field={field_label or 'none'}\n"
        f"Context JSON:\n{json.dumps(ctx, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"User asked: \"{question}\""
    )
    res = call_gemini(
        [prompt], model=env("GEMINI_TEXT_MODEL"), key=KEY, schema=Answer, temperature=0.2,
        timeout_s=12.0, thinking_level="low",
        system_instruction=SYSTEM.format(lang_name=LANG_NAME.get(lang, LANG_NAME["hi"])),
    )
    a: Answer = res.parsed
    log.info("answer model=%s confident=%s sources=%d ctx_services=%d", res.model, a.confident, len(a.sources), len(ctx["services"]))
    return a


async def answer(question: str, lang: str, page_title: str | None = None, field_label: str | None = None) -> Answer | None:
    try:
        return await asyncio.to_thread(answer_sync, question, lang, page_title, field_label)
    except (LLMError, ValueError) as e:
        log.warning("answer failed: %s", e)
        return None
