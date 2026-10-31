"""Field-by-field map of the demo form (PLAYBOOK §7.2) → data/forms/<process_id>.json."""
from __future__ import annotations

from enum import Enum

from .common import StrictModel


class FieldType(str, Enum):
    text = "text"
    number = "number"
    date = "date"
    dropdown = "dropdown"
    radio = "radio"
    checkbox = "checkbox"
    file = "file"
    otp = "otp"
    password = "password"


class FieldDocument(StrictModel):
    name: str
    formats: list[str] = []
    max_size_kb: int | None = None


class CommonError(StrictModel):
    message_contains: str  # exact (sub)string of the portal's error text
    fix: str


class FormField(StrictModel):
    field_id: str
    order: int
    label: str  # exact label as shown on the live form
    label_aliases: list[str] = []  # other-language labels (Gujarati copied verbatim from portal)
    type: FieldType
    required: bool
    sensitive: bool = False  # Aadhaar number, OTP, password, bank fields → capture pauses
    meaning: str | None = None
    rules: list[str] = []
    format_hint: str | None = None
    options: list[str] | None = None
    documents: list[FieldDocument] = []
    common_errors: list[CommonError] = []
    ask_back: str | None = None  # id of an AskBack
    source_url: str | None = None


class FormPage(StrictModel):
    page_id: str
    order: int
    title: str
    recognise_by: list[str] = []
    is_login: bool = False
    fields: list[FormField] = []


class AskBack(StrictModel):
    id: str
    question: str
    options: list[str]
    affects: list[str] = []  # field_ids


class AfterSubmit(StrictModel):
    next_steps: str | None = None
    timeline: str | None = None
    fee: str | None = None
    tracking_url: str | None = None
    source_url: str | None = None


class FormMap(StrictModel):
    process_id: str  # same value as Service.service_id
    pages: list[FormPage]
    ask_backs: list[AskBack] = []
    pre_submit_checklist: list[str] = []
    after_submit: AfterSubmit = AfterSubmit()
