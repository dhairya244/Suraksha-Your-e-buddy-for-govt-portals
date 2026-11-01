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
from backend.core.llm import REQUEST_TIMEOUT_S, LLMError, call_gemini

log = logging.getLogger("suraksha.vision")

VISION_KEY = "GEMINI_API_KEY_BACKUP"  # vision; answers use GEMINI_API_KEY_DEMO (separate quotas)
VISION_TIMEOUT_S = REQUEST_TIMEOUT_S  # GEMINI_TIMEOUT_SECONDS (API minimum is 10 s)


class FieldKind(str, Enum):
    text = "text"
    number = "number"
    date = "date"
    dropdown = "dropdown"
    radio = "radio"
    checkbox = "checkbox"
    file = "file"
    textarea = "textarea"
    password = "password"  # masked input (dots)
    captcha = "captcha"
    other = "other"


class Marker(str, Enum):
    star = "star"  # '*' next to the label
    optional = "optional"  # the page literally says "(optional)" / "વૈકલ્પિક" / "वैकल्पिक"
    none = "none"


class PageType(str, Enum):
    login = "login"
    registration = "registration"
    service_list = "service_list"
    application_form = "application_form"
    payment = "payment"
    acknowledgement = "acknowledgement"
    other = "other"


class SeenField(BaseModel):
    label: str = Field(description="Field label exactly as printed next to the input (keep original script)")
    kind: FieldKind
    marker: Marker = Field(description="Explicit marker printed with the label; 'none' if nothing is printed")
    filled: bool = Field(description="True only if a real value is typed or a real option chosen (placeholder text does NOT count)")
    focused: bool = Field(description="True if this input has keyboard focus (blinking caret or focus ring), not mere mouse hover")
    box_2d: list[int] = Field(description="[ymin, xmin, ymax, xmax] 0-1000 of the INPUT CONTROL itself (not the label)")


class Action(BaseModel):
    text: str = Field(description="Button or link text exactly as shown")
    box_2d: list[int]


class ErrorText(BaseModel):
    text: str
    box_2d: list[int]


class VisionResult(BaseModel):
    page_type: PageType
    logged_in: bool = Field(description="True if the site shows the user is signed in (Logout link, user name/profile menu)")
    is_form: bool = Field(description="True if a fillable web form is visible in a browser")
    page_title: str = Field(description="Heading of the form/page, '' if none")
    fields: list[SeenField] = Field(description="Visible input controls in reading order (top→bottom, left→right)")
    actions: list[Action] = Field(description="Visible primary buttons/links of the page content: login, register / new registration, submit, save, next, apply, pay. Max 8.")
    errors: list[ErrorText] = Field(description="Visible red validation/error messages")


PROMPT = """You are looking at a screenshot of a browser showing a government website (often Digital Gujarat).
1. page_type: login (sign-in form), registration (new account sign-up), service_list (list/search of services or a service info page),
   application_form (a service application being filled), payment, acknowledgement (submitted / receipt), other.
2. logged_in: is the user signed in on this site?
3. List every visible input control (text box, dropdown, date, radio group, checkbox, file upload, textarea, password, captcha)
   in reading order: top to bottom, and left to right within a row.
- box_2d must tightly cover the input control itself (the box/dropdown/radio group), NOT the label text.
- For a radio group, box the whole group of options as one field.
- label: copy the label text exactly (Gujarati, Hindi or English as printed). Drop the trailing '*'.
  If the control has no label, use its placeholder text (e.g. 'User Name', 'Enter Captcha'); for a dropdown whose only text is
  'Select', name it by its purpose from nearby text or icon, e.g. 'Login type (Select)'.
- kind: a <select>-style box with an arrow is 'dropdown' even if its label says 'Mobile No'. Masked dots = 'password'. Distorted text image + box = 'captcha'.
- marker: 'star' only if a '*' is printed with the label, 'optional' only if the page literally says optional; otherwise 'none'. Do not guess.
- filled: true only if a real value is typed or selected. Placeholders like 'Select', '--Select--', 'DD/MM/YYYY' mean NOT filled.
- focused: only a caret or focus ring; mouse hover is NOT focus.
- Ignore browser chrome (address bar, tabs, bookmarks), site navigation menus and site-wide search boxes.
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
    out.actions = [a for a in out.actions if _valid_box(a.box_2d)]
    log.info("vision model=%s page=%s logged_in=%s fields=%d actions=%d errors=%d",
             res.model, out.page_type.value, out.logged_in, len(out.fields), len(out.actions), len(out.errors))
    return out


async def detect(jpeg: bytes, label_hints: list[str] | None = None, own_ui_visible: bool = False) -> VisionResult | None:
    try:
        return await asyncio.to_thread(detect_sync, jpeg, label_hints, own_ui_visible)
    except (LLMError, ValueError) as e:
        log.warning("vision failed: %s", e)
        return None


def _valid_box(b: list[int]) -> bool:
    return len(b) == 4 and 0 <= b[0] < b[2] <= 1000 and 0 <= b[1] < b[3] <= 1000
