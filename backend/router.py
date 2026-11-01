"""Question routing by rules (no LLM): control words, pending yes/no, scheme talk; the rest → answer call.

The answer call itself labels page_question / service_question / general (Answer.kind).
Control and yes/no only fire on SHORT utterances, so "is page par kya bharna hai" is a question, not "yes".
"""
from __future__ import annotations

import re
from enum import Enum


class Route(str, Enum):
    next = "next"
    repeat = "repeat"
    stop = "stop"
    yes = "yes"
    no = "no"
    scheme = "scheme"
    need = "need"  # stated need for a covered service → task goal (decided in server via backend/task.py)
    answer = "answer"  # page / service / general → screen-aware answer call


SHORT_WORDS = 5

NEXT_RE = re.compile(r"^\s*(next|agla|agle|aage( badho| chalo)?|आगे( बढ़ो| चलो)?|अगला|अगले|આગળ)\b", re.I)
REPEAT_RE = re.compile(r"repeat|again|dobara|phir se|fir se|फिर से|दोबारा|दुबारा|ફરી", re.I)
STOP_RE = re.compile(r"^\s*(stop|ruko|ruk jao|band karo|bas|रुको|रुक जाओ|बंद करो|बस|બંધ)\b", re.I)
YES_RE = re.compile(r"\b(haan|han|haa|ha|yes|ji|jee|hanji)\b|हाँ|हां|जी|હા", re.I)
HAVE_RE = re.compile(r"\bhai\b|है|છે|\bhave\b", re.I)
ACCOUNT_RE = re.compile(r"account|acount|अकाउंट|खाता|ખાતું|એકાઉન્ટ", re.I)  # "account hai" = yes; bare "hai" too
NO_RE = re.compile(r"\b(nahi|nahin|nai|no|nope|na)\b|नहीं|नही|(?<!\S)ना(?!\S)|ના|નથી", re.I)
SCHEME_RE = re.compile(r"scheme|yojana|yojna|योजना|યોજના|subsidy|सब्सिडी|sahay|सहाय|benefit|लाभ|scholarship|छात्रवृत्ति|pension|पेंशन", re.I)
QUESTION_RE = re.compile(r"\?|kya|kais[aei]|kaun|kitna|kab|kahan|kyun|क्या|कैस[ाीे]|कौन|कितन|कब|कहाँ|क्यों|what|how|which|when|where|why", re.I)


def words(text: str) -> int:
    return len(text.split())


def yes_no(text: str) -> Route | None:
    """Spoken answer to a pending yes/no question — short utterances only, never a question."""
    if words(text) > SHORT_WORDS or QUESTION_RE.search(text):
        return None
    no = bool(NO_RE.search(text))
    have = bool(HAVE_RE.search(text)) and (bool(ACCOUNT_RE.search(text)) or words(text) == 1)
    yes = bool(YES_RE.search(text)) or (not no and have)
    if yes != no:
        return Route.yes if yes else Route.no
    return None


def route(text: str, *, pending_yes_no: bool, scheme_active: bool) -> Route:
    t = text.strip()
    if pending_yes_no and (yn := yes_no(t)):
        return yn
    if words(t) <= SHORT_WORDS and not QUESTION_RE.search(t):
        if STOP_RE.search(t):
            return Route.stop
        if NEXT_RE.search(t):
            return Route.next
        if REPEAT_RE.search(t):
            return Route.repeat
    if SCHEME_RE.search(t) or scheme_active:
        return Route.scheme
    return Route.answer
