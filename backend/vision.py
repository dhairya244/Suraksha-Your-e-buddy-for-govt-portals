"""Gemini vision: screenshot → visible form fields with boxes (PLAYBOOK §8.4, simplified for M2).

Boxes are Gemini-native [ymin, xmin, ymax, xmax] on a 0–1000 grid relative to the image.
Works without a form map (general mode); when one exists, its labels are passed as hints.
"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum

from google.genai import types
from pydantic import BaseModel, Field

from backend.config import env
from backend.core.llm import LLMError, call_gemini

log = logging.getLogger("suraksha.vision")

VISION_KEY = "GEMINI_API_KEY_DEMO"
VISION_TIMEOUT_S = 10.0


class FieldKind(str, Enum):
    text = "text"
    number = "number"
    date = "date"
    dropdown = "dropdown"
    radio = "radio"
    checkbox = "checkbox"
    file = "file"
    textarea = "textarea"
    other = "other"


class SeenField(BaseModel):
    label: str = Field(description="Field label exactly as printed next to the input (keep original script)")
    kind: FieldKind
    required: bool = Field(description="True if marked with * or 'required'")
    filled: bool = Field(description="True if the input already has a user value or a chosen option (placeholder text does NOT count)")
    sensitive: bool = Field(description="Aadhaar number, OTP, password, PAN, bank account/IFSC")
    box_2d: list[int] = Field(description="[ymin, xmin, ymax, xmax] 0-1000 of the INPUT CONTROL itself (not the label)")


class ErrorText(BaseModel):
    text: str
    box_2d: list[int]


class VisionResult(BaseModel):
    is_form: bool = Field(description="True if a fillable web form is visible in a browser")
    page_title: str = Field(description="Heading of the form/page, '' if none")
    fields: list[SeenField] = Field(description="Visible input controls in reading order (top→bottom, left→right)")
    errors: list[ErrorText] = Field(description="Visible red validation/error messages")


PROMPT = """You are looking at a screenshot of a computer screen. Find the web form shown in the browser.
List every visible input control (text box, dropdown, date, radio group, checkbox, file upload, textarea)
in reading order: top to bottom, and left to right within a row.
- box_2d must tightly cover the input control itself (the box/dropdown/radio group), NOT the label text.
- For a radio group, box the whole group of options as one field.
- label: copy the label text exactly (Gujarati, Hindi or English as printed). Drop the trailing '*'.
- filled: true only if a real value is typed or selected. Placeholders like 'Select', '--Select--', 'DD/MM/YYYY' mean NOT filled.
- Ignore browser chrome (address bar, tabs, bookmarks), site navigation menus, search boxes, and login widgets unless the page is a login form.
- Ignore any floating assistant window.
{own_ui}{hint}"""

OWN_UI = "- Ignore any orange highlight outline, its dark caption label, and the dark 'Suraksha' pill window — they are our own UI, not part of the page.\n"


def _hint(label_hints: list[str] | None) -> str:
    if not label_hints:
        return ""
    return "Labels expected on this form (may be partial): " + "; ".join(label_hints[:60])


def detect_sync(jpeg: bytes, label_hints: list[str] | None = None, own_ui_visible: bool = False) -> VisionResult:
    res = call_gemini(
        [types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"), PROMPT.format(hint=_hint(label_hints), own_ui=OWN_UI if own_ui_visible else "")],
        model=env("GEMINI_VISION_MODEL"),
        key=VISION_KEY,
        schema=VisionResult,
        temperature=0.0,
        timeout_s=VISION_TIMEOUT_S,
        thinking_level="low",
    )
    out: VisionResult = res.parsed
    out.fields = [f for f in out.fields if _valid_box(f.box_2d)]
    out.errors = [e for e in out.errors if _valid_box(e.box_2d)]
    log.info("vision model=%s fields=%d errors=%d", res.model, len(out.fields), len(out.errors))
    return out


async def detect(jpeg: bytes, label_hints: list[str] | None = None, own_ui_visible: bool = False) -> VisionResult | None:
    try:
        return await asyncio.to_thread(detect_sync, jpeg, label_hints, own_ui_visible)
    except (LLMError, ValueError) as e:
        log.warning("vision failed: %s", e)
        return None


def _valid_box(b: list[int]) -> bool:
    return len(b) == 4 and 0 <= b[0] < b[2] <= 1000 and 0 <= b[1] < b[3] <= 1000
