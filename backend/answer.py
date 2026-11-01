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
from backend.core.llm import REQUEST_TIMEOUT_S, LLMError, call_gemini
from backend.knowledge import pick_context
from backend.task import ALL_STEPS_RE, SCREEN_RE

log = logging.getLogger("suraksha.answer")

KEY = "GEMINI_API_KEY_DEMO"  # answers; vision uses GEMINI_API_KEY_BACKUP (separate quotas)
TIMEOUT_S = REQUEST_TIMEOUT_S  # GEMINI_TIMEOUT_SECONDS
LANG_NAME = {"hi": "Hindi (Devanagari script)", "en": "simple Indian English", "gu": "Gujarati (Gujarati script)",
             "mr": "Marathi (Devanagari script)", "bn": "Bengali (Bengali script)", "ta": "Tamil (Tamil script)",
             "te": "Telugu (Telugu script)", "kn": "Kannada (Kannada script)", "ml": "Malayalam (Malayalam script)",
             "pa": "Punjabi (Gurmukhi script)", "od": "Odia (Odia script)"}

SYSTEM = """You are e-Sahayak (say "ई-सहायक" in Hindi, "ઈ-સહાયક" in Gujarati, "e-Sahayak" in English), a voice assistant sitting next to someone who is filling a Gujarat government form
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

GOAL + ONE STEP: `task.goal` is the service the user is working on and `task.current_step` the ONE next action on this
screen. For "how do I apply / what now" give only that one step (and at most one short sentence of what comes after).
Never list all the steps unless all_steps_requested is true. Do not explain Digital Gujarat registration or login
unless the user asks about registration/login/account.

LINK ONLY: a service in the context with "details": null (or no documents/steps) but an official_url → say we don't have
verified details for it yet and that the official website link is in the pill; put its official_url in sources.

NOT COVERED: if the user wants a service that is not in the context JSON (e.g. driving licence, Startup India), say in
ONE short sentence that you (e-Sahayak) cannot guide that service here and that they should use that service's own official
website. Nothing else (no question back). Do not invent links, fees or steps for it. Set about_goal=false.

FOLLOW-UPS: short questions like "kyun? / aur? / aur koi? / iske documents? / iske liye … / kahan milega? / kaise?" refer to
`task.last_answer_topic` (the previous answer), NOT to the screen — e.g. after a documents answer, "iske liye kahan se
milega?" = where to get THOSE documents. Only when mentions_screen is true ("yahan / is page / is form") answer about the screen.
For "where do I get it" name only documents/offices that appear in the context; if the context does not say, say so in one
sentence (e.g. "the official form doesn't say which office") instead of guessing.

Never add a "back to the form / चलिए, वापस …" sentence yourself — the app adds it.

QUESTION KINDS (set `kind`):
- page_question → answer from the screenshot + session state (what to fill/click here, in order).
- service_question → answer from the context JSON with sources.
- general / off-topic (weather, jokes, other topics) → one short polite sentence that you can only help with this
  form and government services. No facts, empty sources. Do not offer to continue / ask "shall we continue?" —
  when the user has a goal the app itself adds the line that brings them back to it.

TONE: always polite, gentle and respectful — "कृपया", "आप", "…भरिए / …दबाइए / …कीजिए"; never bare commands like
"दबाओ / भरो / करो". English: "please …". Gujarati: "કૃપા કરીને", "તમે". Keep official terms and quotes exactly as in the source.

STYLE: spoken aloud — plain everyday words, at most 3 short sentences (a list of up to 6 documents is fine),
no markdown, no URLs in `say`. Reply in {lang_name}; English names of portal buttons/labels are fine.
Hindi must be written in Devanagari script only (never romanized Hindi). You speak with a female voice:
use feminine forms ONLY for yourself (मैं मदद कर सकती हूँ). The user's gender is unknown: always address them
with आप + respectful plural forms (आप भर सकते हैं, आप पात्र हो सकते हैं) — never सकती हैं / रही हैं for the user."""


class Answer(BaseModel):
    kind: Literal["page_question", "service_question", "general"] = Field(
        description="page_question: about what is on screen; service_question: documents/fees/steps/eligibility/timeline; general: anything else")
    say: str = Field(description="What to speak to the user")
    topic: str = Field(description="Short English label of what this answer is about, e.g. 'Income Certificate documents', 'driving licence (not covered)'")
    about_goal: bool = Field(description="True if the answer is about the user's current goal service / this screen; false for side topics")
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
    task: dict = field(default_factory=dict)  # goal, current_step, last_answer_topic (backend/task.py)

    def as_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], {})}


def answer_sync(question: str, lang: str, screen: ScreenContext, jpeg: bytes | None, turns: list[dict] | None = None) -> Answer:
    history = " ".join(t["text"] for t in (turns or [])[-4:])  # follow-ups name topics from earlier turns
    ctx = pick_context(f"{history} {screen.task.get('last_answer_topic', '')} {question}", screen.service_id)
    flags = {"all_steps_requested": bool(ALL_STEPS_RE.search(question)), "mentions_screen": bool(SCREEN_RE.search(question))}
    convo = "\n".join(f"{t['role']}: {t['text']}" for t in (turns or []))
    text = (
        f"Conversation so far (oldest first; resolve follow-ups like 'aur koi?', 'iske documents?' against it):\n{convo or '(none)'}\n\n"
        f"Session state: {json.dumps(screen.as_json(), ensure_ascii=False)}\n"
        f"Screenshot attached: {'yes' if jpeg else 'no (screen not captured right now)'}\n"
        f"Context JSON (official data):\n{json.dumps(ctx, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"Flags: {json.dumps(flags)}\n"
        f"User asked: \"{question}\""
        f"\n\nREPLY LANGUAGE: {LANG_NAME.get(lang, LANG_NAME['hi'])} only — the user asked in this language; ignore the language of earlier turns."
    )
    parts: list = [types.Part.from_bytes(data=jpeg, mime_type="image/jpeg")] if jpeg else []
    parts.append(text)
    res = call_gemini(
        parts, model=env("GEMINI_TEXT_MODEL"), key=KEY, schema=Answer, temperature=0.2,
        timeout_s=TIMEOUT_S, thinking_level="low", backup_key="GEMINI_API_KEY_BACKUP",
        system_instruction=SYSTEM.format(lang_name=LANG_NAME.get(lang, LANG_NAME["hi"])),
    )
    a: Answer = res.parsed
    log.info("answer model=%s kind=%s about_goal=%s confident=%s sources=%d image=%s page=%s",
             res.model, a.kind, a.about_goal, a.confident, len(a.sources), bool(jpeg), screen.page_type)
    return a


async def answer(question: str, lang: str, screen: ScreenContext | None = None, jpeg: bytes | None = None,
                 turns: list[dict] | None = None) -> Answer | None:
    try:
        return await asyncio.to_thread(answer_sync, question, lang, screen or ScreenContext(), jpeg, turns)
    except (LLMError, ValueError) as e:
        log.warning("answer failed: %s", e)
        return None
