"""Task engine: the user's GOAL (a service) and the ONE next STEP toward it on the current screen.

Explicit state, no LLM:
  goal          service_id the user said they want; persists until done / Reset / an explicit, confirmed switch
  pending_switch service_id waiting for "yes" ("Income certificate chhod kar caste certificate shuru karein?")
  step          one action on THIS screen: service card → "Apply Online" → login → form fields → uploads → submit
  last_topic    what the previous answer was about (follow-ups resolve against it)

Navigation steps (card / apply / submit) are highlighted and spoken ONCE per page, then we wait for the
screen to change. Field steps come from the Tracker (server.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.knowledge import services
from backend.vision import Action, PageType, VisionResult

# "I want / banana hai / chahiye / apply karna hai" — a stated need (not a question)
NEED_RE = re.compile(
    r"\b(i want|i need|want to|need to|have to|apply for|make my|create my|get my)\b"
    r"|\b(banana|banwana|banvana|banane|banwane|nikalna|nikalwana|chahiye|chaiye|karna|karwana|apply)\b"
    r"|बनाना|बनवाना|बनाने|बनवाने|निकालना|निकलवाना|चाहिए|करना है|करवाना|अप्लाई"
    r"|બનાવવું|બનાવવો|કઢાવવો|જોઈએ",
    re.I,
)
# question words: "kaun se documents chahiye" is a question, not a new goal
ASK_RE = re.compile(r"\?|\b(kya|kaun|kaise|kitna|kab|kahan|kahaan|kyun|kyon|what|how|which|when|where|why)\b"
                    r"|क्या|कौन|कैसे|कितन|कब|कहाँ|कहां|क्यों", re.I)
DOCS_RE = re.compile(r"document|dastavez|kagaz|कागज|दस्तावेज|દસ્તાવેજ", re.I)
ALL_STEPS_RE = re.compile(r"saare steps|sare steps|sab steps|all (the )?steps|सारे स्टेप|सभी स्टेप|सारे चरण|पूरी प्रक्रिया|poori process|full process", re.I)
SCREEN_RE = re.compile(r"\byahan\b|\byaha\b|\bis page\b|\bis form\b|\bthis page\b|\bthis form\b|\bhere\b|यहाँ|यहां|इस पेज|इस फ़ॉर्म|इस फॉर्म|અહીં", re.I)

APPLY_RE = re.compile(r"apply\s*online|apply\s*now|^\s*apply\s*$|online\s*apply|ઓનલાઇન અરજી|ઓનલાઈન અરજી|ऑनलाइन आवेदन", re.I)
SUBMIT_RE = re.compile(r"submit|save\s*(&|and)?\s*(next|continue)?|confirm|proceed|સબમિટ|जमा", re.I)

# Goal keyword families: user words → service ids. Order matters (specific before generic).
FAMILIES: list[tuple[str, str, str]] = [
    # (regex over the user's words, service_id, short name used in speech/cards)
    (r"unreserved|bin ?anamat|बिन ?अनामत|બિન ?અનામત|general caste", "gj_unreserved_caste_certificate_urban", "Unreserved Caste Certificate"),
    (r"non.?creamy|नॉन ?क्रीमी|क्रीमी लेयर", "gj_non_creamy_layer_certificate_for_gujarat_government", "Non-Creamy Layer Certificate"),
    (r"\bews\b|economically weaker|ईडब्ल्यूएस|आर्थिक रूप से कमजोर", "gj_eligibility_certificate_for_economically_weaker_sections_with_income", "EWS Certificate"),
    (r"\bsebc\b|\bobc\b|ओबीसी", "gj_sebc_certificate", "SEBC Certificate"),
    (r"\bst\b caste|scheduled tribe|अनुसूचित जनजाति", "gj_st_scheduled_tribe_caste_certificate", "ST Caste Certificate"),
    (r"\bsc\b caste|scheduled caste|अनुसूचित जाति", "gj_sc_caste_certificate", "SC Caste Certificate"),
    (r"caste|jati|jaati|जाति|જાતિ|જ્ઞાતિ", "gj_unreserved_caste_certificate_urban", "Caste Certificate"),
    (r"income|aay|aavak|aawak|आय|आमदनी|આવક", "gj_income_certificate", "Income Certificate"),
    (r"ration|राशन|રેશન", "gj_application_for_new_ration_card", "New Ration Card"),
    (r"minority|अल्पसंख्यक", "gj_religious_minority_certificate_urban", "Religious Minority Certificate"),
    (r"new pan|pan card|पैन कार्ड|\bpan\b", "central_pan_new", "PAN Card"),
]

# words that identify the goal's card on the services list (lower-case substrings of the card text)
CARD_WORDS: dict[str, list[str]] = {
    "gj_income_certificate": ["income certificate", "આવકનો દાખલો", "आय प्रमाण"],
    "gj_unreserved_caste_certificate_urban": ["unreserved", "બિન અનામત"],
    "gj_non_creamy_layer_certificate_for_gujarat_government": ["non-creamy", "non creamy"],
    "gj_eligibility_certificate_for_economically_weaker_sections_with_income": ["economically weaker", "ews"],
    "gj_sebc_certificate": ["sebc"],
    "gj_st_scheduled_tribe_caste_certificate": ["st (scheduled tribe)", "scheduled tribe"],
    "gj_sc_caste_certificate": ["sc caste", "scheduled caste"],
    "gj_application_for_new_ration_card": ["new ration card"],
    "gj_religious_minority_certificate_urban": ["religious minority"],
}
# card text that means a DIFFERENT variant than the goal (don't highlight "Income Certificate (Panchayat)" for the base one)
VARIANT_WORDS = ["panchayat", "rural", "ગ્રામ", "directorate", "alternate", "hostel", "municipal"]


def find_service(text: str) -> tuple[str, str] | None:
    """(service_id, short name) the user's words name, if we cover it."""
    t = text.lower()
    for rx, sid, name in FAMILIES:
        if re.search(rx, t, re.I) and sid in services():
            return sid, name
    return None


def is_need(text: str) -> bool:
    """A stated need ("income certificate banana hai", "I want to create…"), not a question."""
    return bool(NEED_RE.search(text)) and not ASK_RE.search(text) and not DOCS_RE.search(text)


@dataclass
class Step:
    kind: str  # card | find_card | apply | login | field | submit | done | none
    label: str = ""
    box: list[int] | None = None


@dataclass
class Task:
    goal: str | None = None
    goal_name: str = ""
    pending_switch: tuple[str, str] | None = None
    step: Step = field(default_factory=lambda: Step("none"))
    last_topic: str | None = None

    # ------------------------------------------------------------- goal
    def set_goal(self, sid: str, name: str) -> None:
        self.goal, self.goal_name = sid, name
        self.pending_switch = None
        self.step = Step("none")

    def same_family(self, sid: str) -> bool:
        if not self.goal:
            return False
        if sid == self.goal:
            return True
        fam = lambda s: "caste" if "caste" in s else s.split("_certificate")[0]
        return fam(sid) == fam(self.goal)

    # ------------------------------------------------------------- navigation step on this screen
    def nav_step(self, r: VisionResult) -> Step | None:
        """Card / Apply Online / Submit on non-form pages, for the current goal. None → not a nav page."""
        if r.page_type in (PageType.login, PageType.registration, PageType.application_form,
                           PageType.payment, PageType.acknowledgement):
            return None
        applies = [a for a in r.actions if APPLY_RE.search(a.text)]
        card = self._goal_card(r.actions) if self.goal else None
        if card and applies:  # list with an Apply button per row → the one on the goal's row
            cy = (card.box_2d[0] + card.box_2d[2]) / 2
            row = [a for a in applies if abs((a.box_2d[0] + a.box_2d[2]) / 2 - cy) < 40]
            if row:
                return Step("apply", row[0].text, row[0].box_2d)
            return Step("card", card.text, card.box_2d)
        if card:
            return Step("card", card.text, card.box_2d)
        if applies:  # service page (the card was clicked): Apply Online is the way forward
            return Step("apply", applies[0].text, applies[0].box_2d)
        if self.goal and r.page_type == PageType.service_list:
            return Step("find_card", self.goal_name)
        return None

    def _goal_card(self, actions: list[Action]) -> Action | None:
        words = CARD_WORDS.get(self.goal or "", [self.goal_name.lower()])
        hits = [a for a in actions if any(w in a.text.lower() for w in words) and not APPLY_RE.search(a.text)]
        exact = [a for a in hits if not any(v in a.text.lower() for v in VARIANT_WORDS)]
        return (exact or hits or [None])[0]

    @staticmethod
    def _find(actions: list[Action], rx: re.Pattern) -> Action | None:
        return next((a for a in actions if rx.search(a.text)), None)

    # ------------------------------------------------------------- speech
    def step_text(self, lang: str) -> str:
        s = self.step
        hi = lang != "en"
        if s.kind == "card":
            return f"\"{s.label}\" पर क्लिक कीजिए" if hi else f"click \"{s.label}\""
        if s.kind == "find_card":
            return (f"सूची में \"{s.label}\" ढूँढिए — दिखे नहीं तो नीचे स्क्रॉल कीजिए" if hi
                    else f"find \"{s.label}\" in the list — scroll down if you can't see it")
        if s.kind == "apply":
            return f"\"{s.label}\" बटन दबाइए" if hi else f"press \"{s.label}\""
        if s.kind == "login":
            return "लॉगिन कीजिए" if hi else "log in"
        if s.kind == "field":
            return f"\"{s.label}\" भरिए" if hi else f"fill \"{s.label}\""
        if s.kind == "submit":
            return (f"सब जाँच कर \"{s.label}\" आप खुद दबाइए" if hi else f"check everything, then press \"{s.label}\" yourself")
        return ""

    def describe(self) -> dict:
        """Goal + current step for the answer prompt."""
        out: dict = {}
        if self.goal:
            out["goal"] = {"service_id": self.goal, "name": self.goal_name}
        if self.step.kind != "none":
            out["current_step"] = self.step_text("en")
        if self.last_topic:
            out["last_answer_topic"] = self.last_topic
        return out
