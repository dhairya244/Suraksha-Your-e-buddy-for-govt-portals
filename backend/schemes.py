"""Scheme dialog: slot filling → deterministic filter over data/schemes.json → grounded reply.

Slots: state, occupation, category, gender, age, income (annual family, ₹), student.
- Ask ONE missing slot at a time, and only a slot that changes which schemes match (max 2 questions).
- Requirements come from tags + eligibility text (income/age limits parsed from the scheme's own lines).
- Results: top 2–3 with a one-line reason tied to an eligibility line + official_url; "confirm on the official page".
- Follow-ups: "aur koi?" → next matches; "iske documents?" → that scheme's documents; "kaise apply karu?" → how_to_apply;
  required_service_ids (e.g. income certificate) → offer to guide that form.
LLM is used only to (1) read the user's reply into slots/intent and (2) word the answer from the selected data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from backend.config import env
from backend.core.llm import LLMError, call_gemini
from backend.knowledge import schemes as all_schemes
from backend.knowledge import services

log = logging.getLogger("suraksha.schemes")

KEY = "GEMINI_API_KEY_DEMO"
TIMEOUT_S = 12.0
PAGE_SIZE = 2
MAX_QUESTIONS = 2
SLOT_ORDER = ["occupation", "student", "gender", "category", "age", "income", "state"]
LABOUR_RE = re.compile(r"labour welfare|gbocwwb|glwb|shram|construction worker", re.I)

ASK = {
    "hi": {
        "occupation": "आप क्या काम करते हैं — खेती, पढ़ाई, मज़दूरी या नौकरी, अपना काम-धंधा, या कुछ और?",
        "student": "क्या आप अभी पढ़ाई कर रहे हैं?",
        "gender": "यह योजना महिलाओं के लिए भी हो सकती है — आप महिला हैं या पुरुष?",
        "category": "आपकी श्रेणी क्या है — SC, ST, OBC, General या EWS?",
        "age": "आपकी उम्र कितनी है?",
        "income": "आपके परिवार की सालाना आय लगभग कितनी है?",
        "state": "आप किस राज्य में रहते हैं?",
        "none": "आपकी जानकारी से मेल खाती कोई योजना मेरे पास नहीं मिली। myScheme या Digital Gujarat पर और योजनाएँ देख सकते हैं।",
        "no_more": "मेरे पास इससे ज़्यादा मेल खाती योजनाएँ नहीं हैं।",
        "offer_service": "इसके लिए {service} चाहिए — मैं उसका फ़ॉर्म भरने में मदद कर सकती हूँ।",
        "offer_here": "इसके लिए {service} चाहिए — आप अभी वही फ़ॉर्म भर रहे हैं, चलिए जारी रखते हैं।",
    },
    "en": {
        "occupation": "What do you do — farming, studying, labour or a job, your own business, or something else?",
        "student": "Are you studying right now?",
        "gender": "Some schemes are for women — are you a woman or a man?",
        "category": "What is your category — SC, ST, OBC, General or EWS?",
        "age": "How old are you?",
        "income": "Roughly what is your family's yearly income?",
        "state": "Which state do you live in?",
        "none": "I couldn't find a matching scheme in my list. You can check more schemes on myScheme or Digital Gujarat.",
        "no_more": "I don't have more matching schemes.",
        "offer_service": "This needs a {service} — I can help you fill that form.",
        "offer_here": "This needs a {service} — you're filling that form right now, let's continue.",
    },
}


def ask_text(lang: str, key: str, **kw) -> str:
    return ASK.get(lang, ASK["en"])[key].format(**kw)


# ------------------------------------------------------------------ requirements
_NUM = r"(\d[\d,]*(?:\.\d+)?)"


def _rupees(text: str) -> list[int]:
    out = []
    for m in re.finditer(r"(?:₹|rs\.?|inr)\s*" + _NUM + r"(\s*lakh)?|" + _NUM + r"\s*lakh", text, re.I):
        num = m.group(1) or m.group(3)
        val = float(num.replace(",", ""))
        if m.group(2) or (m.group(3) and not m.group(1)):
            val *= 100000
        out.append(int(val))
    return out


def requirements(s: dict) -> dict:
    """slot → predicate parameters for one scheme (only slots the scheme actually constrains)."""
    tags = set(s.get("tags") or [])
    req: dict = {}
    if "farmer" in tags:
        req["occupation"] = {"farmer"}
    elif LABOUR_RE.search(s.get("name", "")) and "student" not in tags:
        req["occupation"] = {"worker"}
    if "student" in tags:
        req["student"] = True
    if "women" in tags:
        req["gender"] = "female"
    if "sc_st" in tags:
        req["category"] = {"SC", "ST"}
    elif "obc" in tags:
        req["category"] = {"OBC"}
    if "senior_citizen" in tags:
        req["age"] = (60, 200)
    for line in s.get("eligibility") or []:
        low = line.lower()
        if "income" in low:
            amounts = _rupees(line)
            if amounts and re.search(r"not exceed|below|less than|up ?to|must be|should not", low):
                req["income"] = max(amounts)  # urban/rural variants → the higher limit
        m = re.search(r"(?:between|aged?)\s*(\d{1,2})\s*(?:-|to|and)\s*(\d{1,3})\s*years", low)
        if m:
            req["age"] = (int(m.group(1)), int(m.group(2)))
        elif (m := re.search(r"(\d{1,2})\s*years (?:of age )?(?:or|and) (?:above|older)", low)):
            req["age"] = (int(m.group(1)), 200)
    if s.get("level") == "state" and s.get("state"):
        req["state"] = s["state"].lower()
    return req


def check(slot: str, want, have) -> bool:
    if slot == "occupation":
        return have in want
    if slot == "student":
        return bool(have) == bool(want)
    if slot == "gender":
        return have == want
    if slot == "category":
        return have in want
    if slot == "age":
        return want[0] <= int(have) <= want[1]
    if slot == "income":
        return int(have) <= want
    if slot == "state":
        return str(have).lower() == want
    return True


@dataclass
class Match:
    scheme: dict
    satisfied: list[str]
    unknown: list[str]

    @property
    def evidence(self) -> list[str]:
        return [x for x in self.satisfied if x != "state"]

    @property
    def full(self) -> bool:
        return not self.unknown


def match(slots: dict) -> list[Match]:
    out = []
    for s in all_schemes():
        req = requirements(s)
        sat, unk, ok = [], [], True
        for slot, want in req.items():
            have = slots.get(slot)
            if have is None:
                unk.append(slot)
            elif check(slot, want, have):
                sat.append(slot)
            else:
                ok = False
                break
        if ok:
            out.append(Match(s, sat, unk))
    # most specific positive evidence first, then fewest unknowns
    # most specific positive evidence first, then fewest unknowns. 'state' is a filter, not evidence
    # (it's defaulted to Gujarat), so a scheme needs at least one matched personal slot to be suggested.
    out.sort(key=lambda m: (-len(m.evidence), len(m.unknown), m.scheme["name"]))
    return [m for m in out if m.evidence]


def deciding_slot(slots: dict, asked: set[str]) -> str | None:
    """The missing slot that most changes the current candidate list (None → enough to answer)."""
    cands = match(slots)
    if any(m.full for m in cands[:PAGE_SIZE]) and slots.get("occupation"):
        return None
    if not slots.get("occupation") and "occupation" not in asked:
        return "occupation"  # nothing matches with zero evidence; occupation splits the list most
    counts = {slot: sum(1 for m in cands if slot in m.unknown) for slot in SLOT_ORDER}
    best = max((s for s in SLOT_ORDER if s not in slots and s not in asked), key=lambda s: counts[s], default=None)
    return best if best and counts[best] > 0 else None


# ------------------------------------------------------------------ LLM: read reply, word answer
class Slots(BaseModel):
    state: str | None = None
    occupation: Literal["farmer", "student", "worker", "business", "salaried", "homemaker", "unemployed", "other"] | None = None
    category: Literal["SC", "ST", "OBC", "General", "EWS"] | None = None
    gender: Literal["female", "male", "other"] | None = None
    age: int | None = None
    income: int | None = Field(None, description="annual family income in rupees")
    student: bool | None = None


class Read(BaseModel):
    intent: Literal["info", "more", "documents", "how_to_apply", "eligibility", "benefits", "exit"] = Field(
        description="info: gives personal details or asks for schemes; more: wants other/next schemes ('aur koi?'); "
                    "documents/how_to_apply/eligibility/benefits: about a scheme already mentioned; "
                    "exit: not about schemes (e.g. about the form on screen, PAN, other topics)")
    slots: Slots
    scheme_id: str | None = Field(description="scheme_id the user refers to ('iske', 'pehli wali'), from the shown list")


READ_SYS = """Extract what the user said in a government-scheme conversation. Only set slots the user actually stated
(e.g. 'main student hoon' → occupation=student, student=true; 'kisaan hoon' → occupation=farmer; 'mazdoor/labour/construction'
→ worker; 'mahila' → female; '2 lakh' → income=200000). Leave others null. Never guess."""


class Reply(BaseModel):
    say: str
    sources: list[str]


REPLY_SYS = """You are Suraksha (female voice) telling someone about government schemes, spoken aloud, mid-conversation:
no greeting, no self-introduction. Use ONLY the scheme JSON given.
PRESENTING schemes: mention EVERY scheme in the JSON, each with its name and ONE short reason tied to the eligibility
line that matches the user's details in `matched_on` (e.g. student → the line about students), paraphrased faithfully.
Say they 'may be eligible' and end with one sentence: confirm eligibility on the official page.
DOCUMENTS / HOW TO APPLY / BENEFITS / ELIGIBILITY of one scheme: answer only that, from that field; if it is empty,
say it isn't listed and to check the official page. Don't repeat the eligibility reason or the confirm line.
No URLs in `say`; at most 4 short sentences (a document list may be longer); no markdown.
Reply in {lang_name}. Hindi only in Devanagari. Feminine verb forms ONLY for yourself (मैं बता सकती हूँ).
The user's gender is unknown: address them with आप + the respectful plural forms (आप पात्र हो सकते हैं, आप हैं) — never
सकती हैं / रही हैं for the user."""
LANG_NAME = {"hi": "Hindi", "en": "simple Indian English", "gu": "Gujarati"}


def _call(parts: list, schema, system: str):
    return call_gemini(parts, model=env("GEMINI_TEXT_MODEL"), key=KEY, schema=schema, temperature=0.1,
                       timeout_s=TIMEOUT_S, thinking_level="low", system_instruction=system).parsed


def _brief(s: dict, *fields_: str) -> dict:
    keep = ("scheme_id", "name", "summary", "eligibility", "official_url") + fields_
    return {k: s.get(k) for k in keep if s.get(k) not in (None, [], "")}


# ------------------------------------------------------------------ dialog
@dataclass
class Turn:
    say: str
    sources: list[str] = field(default_factory=list)
    link: dict | None = None  # {url, title} for the pill's Open button
    exit: bool = False  # not a scheme question → caller answers normally


@dataclass
class SchemeDialog:
    # The session is on the Digital Gujarat portal → assume Gujarat unless the user says otherwise.
    slots: dict = field(default_factory=lambda: {"state": "gujarat"})
    asked: set[str] = field(default_factory=set)
    shown: list[str] = field(default_factory=list)  # scheme_ids already presented, in order
    last_batch: list[str] = field(default_factory=list)
    offered: set[str] = field(default_factory=set)  # required services already offered
    active: bool = False

    def _by_id(self, sid: str | None) -> dict | None:
        return next((s for s in all_schemes() if s["scheme_id"] == sid), None)

    def step(self, text: str, lang: str, turns: list[dict], current_service: str | None) -> Turn:
        shown = [{"scheme_id": sid, "name": (self._by_id(sid) or {}).get("name")} for sid in self.last_batch]
        prompt = (f"Known slots: {json.dumps(self.slots)}\nSchemes just shown: {json.dumps(shown, ensure_ascii=False)}\n"
                  f"Recent conversation: {json.dumps(turns[-6:], ensure_ascii=False)}\nUser said: \"{text}\"")
        r: Read = _call([prompt], Read, READ_SYS)
        log.info("scheme read intent=%s slots=%s ref=%s", r.intent, r.slots.model_dump(exclude_none=True), r.scheme_id)
        if r.intent == "exit" and self.active:
            self.active = False
            return Turn(say="", exit=True)
        self.active = True
        new = r.slots.model_dump(exclude_none=True)
        if new.get("occupation") == "student":
            new.setdefault("student", True)
        self.slots.update(new)

        if r.intent in ("documents", "how_to_apply", "eligibility", "benefits") and self.last_batch:
            s = self._by_id(r.scheme_id) or self._by_id(self.last_batch[0])
            fld = {"documents": "documents", "how_to_apply": "how_to_apply", "eligibility": "eligibility", "benefits": "benefits"}[r.intent]
            return self._word(lang, f"The user asks about the {fld} of this scheme.", [_brief(s, fld)], focus=s, current_service=current_service)

        if r.intent == "more":
            rest = [m for m in match(self.slots) if m.scheme["scheme_id"] not in self.shown]
            return self._present(lang, rest, current_service, more=True)

        slot = deciding_slot(self.slots, self.asked)
        if slot and len(self.asked) < MAX_QUESTIONS:
            self.asked.add(slot)
            return Turn(say=ask_text(lang, slot))
        return self._present(lang, match(self.slots), current_service)

    def _present(self, lang: str, ms: list[Match], current_service: str | None, more: bool = False) -> Turn:
        ms = [m for m in ms if m.scheme["scheme_id"] not in self.shown] if more else ms
        if not ms:
            return Turn(say=ask_text(lang, "no_more" if more else "none"))
        batch = ms[:PAGE_SIZE]
        self.last_batch = [m.scheme["scheme_id"] for m in batch]
        self.shown += [sid for sid in self.last_batch if sid not in self.shown]
        data = [dict(_brief(m.scheme), matched_on=m.satisfied, still_unknown=m.unknown) for m in batch]
        task = (f"User details: {json.dumps(self.slots)}. Present these {len(batch)} schemes (best first). "
                "If a scheme has still_unknown slots, say it applies only if that condition is true (e.g. 'if you are SC').")
        return self._word(lang, task, data, focus=batch[0].scheme, current_service=current_service)

    def _word(self, lang: str, task: str, data: list[dict], focus: dict, current_service: str | None) -> Turn:
        rep: Reply = _call([f"Task: {task}\nScheme JSON: {json.dumps(data, ensure_ascii=False)}"], Reply,
                           REPLY_SYS.format(lang_name=LANG_NAME.get(lang, "Hindi")))
        say = rep.say
        req = (focus.get("required_service_ids") or [])
        if req and req[0] not in self.offered:
            self.offered.add(req[0])
            svc = services().get(req[0], {})
            name = svc.get("name") or req[0]
            say += " " + ask_text(lang, "offer_here" if req[0] == current_service else "offer_service", service=name)
        urls = [focus["official_url"]] + [u for u in rep.sources if u != focus["official_url"]]
        return Turn(say=say, sources=urls[:3], link={"url": focus["official_url"], "title": focus["name"]})


async def step(d: SchemeDialog, text: str, lang: str, turns: list[dict], current_service: str | None) -> Turn | None:
    try:
        return await asyncio.to_thread(d.step, text, lang, turns, current_service)
    except (LLMError, ValueError) as e:
        log.warning("scheme step failed: %s", e)
        return None
