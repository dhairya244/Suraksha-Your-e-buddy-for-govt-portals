"""Suraksha backend: WebSocket session that turns screen frames into field highlights (M2).

  .venv/bin/uvicorn backend.server:app --port 8765
  (or) .venv/bin/python -m backend.server

Protocol (matches desktop/src/main.ts):
  client → {type:"start", lang} | {type:"frame", frame_id, jpeg_b64, w, h, big_change, browser} | {type:"next"}
           | {type:"stop"} | {type:"reset", lang?} | {type:"lang", lang} | {type:"ask", audio_b64, lang} | {type:"choice", id, value}
  server → {type:"capture", mode} | {type:"highlight", frame_id, bbox, label} | {type:"clear"}
           | {type:"redact", frame_id, bbox} | {type:"transcript", text, lang, final}
           | {type:"lang", value} | {type:"sources", urls}
           | {type:"choice", id, question, options:[{id,label}]} | {type:"choice.clear"} | {type:"frame.request", reason}
           | {type:"say", text, audio_b64?} | {type:"state", value}
bbox = [ymin, xmin, ymax, xmax] 0–1000 relative to the frame (the client maps it via that frame's crop).
Frames and audio are processed in memory only; nothing is written to disk or logged.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections import deque

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from backend.config import env_int
from backend.vision import PageType, SeenField, VisionResult, detect
from backend.answer import ScreenContext, answer
from backend.knowledge import form_field
from backend.router import Route, route
from backend.schemes import SchemeDialog
from backend.schemes import step as scheme_step
from backend.task import ALL_STEPS_RE, SUBMIT_RE, Step, Task, find_service, is_need
from backend.tracker import Tracker
from backend.voice import stt, tts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("suraksha.server")

VISION_MIN_INTERVAL_S = 4.0  # free-tier budget: ≤1 vision call per 4 s (PLAYBOOK §8.3)
NO_FORM_REPEAT_S = 20.0

app = FastAPI(title="Suraksha")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


# ------------------------------------------------------------------ phrasing (pre-written, no LLM)
T = {
    "hi": {
        "watching": "ठीक है, मैं स्क्रीन देख रही हूँ। फ़ॉर्म वाला पेज खोलिए।",
        "no_form": "मुझे अभी कोई फ़ॉर्म नहीं दिख रहा। फ़ॉर्म वाला पेज खोलिए।",
        "fill": "यहाँ \"{label}\" भरिए।",
        "choose": "यहाँ \"{label}\" में से सही विकल्प चुनिए।",
        "upload": "यहाँ \"{label}\" के लिए दस्तावेज़ अपलोड कीजिए।",
        "tick": "\"{label}\" — अगर लागू हो तो टिक कीजिए।",
        "sensitive": "\"{label}\" निजी जानकारी है — इसे खुद भरिए, मैं स्क्रीन नहीं देखूँगी। हो जाए तो Next दबाइए।",
        "optional": " पेज पर इसे वैकल्पिक लिखा है।",
        "done_visible": "इस हिस्से के सारे खाने भर गए। नीचे स्क्रॉल कीजिए या पेज पर अगला बटन दबाइए।",
        "error": "ध्यान दीजिए, फ़ॉर्म पर गलती दिख रही है: \"{text}\"",
        "stopped": "रुक गई।",
        "not_heard": "माफ़ कीजिए, मैं सुन नहीं पाई। बटन दबाकर रखिए और फिर से बोलिए।",
        "answer_fail": "माफ़ कीजिए, अभी जवाब नहीं मिल पाया। थोड़ी देर में फिर पूछिए।",
        "vision_fail": "स्क्रीन पढ़ने में दिक्कत हुई, फिर से कोशिश कर रही हूँ…",
        "check_in": "कोई दिक्कत? \"{label}\" के बारे में कुछ पूछना हो तो माइक बटन दबाकर पूछिए।",
        "has_account": "क्या आपका Digital Gujarat अकाउंट है?",
        "yes": "हाँ", "no": "नहीं",
        "login_fields": "ठीक है, लॉगिन करते हैं।",
        "register_btn": "आपका अकाउंट नहीं है तो पहले रजिस्टर करना होगा। \"{label}\" बटन दबाइए।",
        "register_find": "आपका अकाउंट नहीं है तो पहले रजिस्टर करना होगा। पेज पर \"New Registration (Citizen)\" ढूँढिए।",
        "login_first": "यह सेवा इस्तेमाल करने के लिए पहले Digital Gujarat पर लॉगिन कीजिए।",
        "payment": "यह पेमेंट पेज है। भुगतान का तरीका चुनिए — Pay बटन आप खुद दबाइए।",
        "ack": "आवेदन जमा हो गया लगता है। आवेदन नंबर लिख लीजिए या रसीद डाउनलोड कर लीजिए।",
        "goal_set": "ठीक है, {name} बनाते हैं।",
        "goal_same": "हम {name} पर ही काम कर रहे हैं।",
        "switch_q": "{old} छोड़कर {new} शुरू करें?",
        "switch_yes": "ठीक है, अब {name}।",
        "switch_no": "ठीक है, {name} जारी रखते हैं।",
        "back": "चलिए, वापस {name} पर — {step}।",
        "nav": "{step}।",
        "goto_list": "इसके लिए Sugam सेवाओं की सूची खोलिए और \"{name}\" चुनिए।",
        "ask_goal": "आपको कौन सी सेवा चाहिए? माइक बटन दबाकर बताइए, जैसे \"आय प्रमाण पत्र बनाना है\"।",
        "submit": "सारे खाने भर गए। एक बार सब जाँच लीजिए, फिर \"{label}\" आप खुद दबाइए।",
    },
    "en": {
        "watching": "Okay, I'm watching the screen. Please open the form page.",
        "no_form": "I can't see a form right now. Please open the form page.",
        "fill": "Fill in \"{label}\" here.",
        "choose": "Choose the right option for \"{label}\" here.",
        "upload": "Upload the document for \"{label}\" here.",
        "tick": "\"{label}\" — tick this if it applies.",
        "sensitive": "\"{label}\" is private — please fill it yourself. Press Next when done.",
        "optional": " The page marks this as optional.",
        "done_visible": "Everything visible here is filled. Scroll down or press the next button on the page.",
        "error": "Heads up, the form shows an error: \"{text}\"",
        "stopped": "Stopped.",
        "not_heard": "Sorry, I didn't catch that. Hold the button and ask again.",
        "answer_fail": "Sorry, I couldn't get an answer right now. Please ask again in a moment.",
        "vision_fail": "Had trouble reading the screen, retrying…",
        "check_in": "Any trouble? If you have a question about \"{label}\", hold the mic button and ask.",
        "has_account": "Do you have a Digital Gujarat account?",
        "yes": "Yes", "no": "No",
        "login_fields": "Okay, let's log in.",
        "register_btn": "Without an account you need to register first. Press the \"{label}\" button.",
        "register_find": "Without an account you need to register first. Look for \"New Registration (Citizen)\" on the page.",
        "login_first": "Please log in to Digital Gujarat first to use this service.",
        "payment": "This is the payment page. Choose a payment method — you press Pay yourself.",
        "ack": "Your application looks submitted. Note down the application number or download the receipt.",
        "goal_set": "Okay, let's get your {name}.",
        "goal_same": "We're already working on the {name}.",
        "switch_q": "Leave the {old} and start the {new} instead?",
        "switch_yes": "Okay, now the {name}.",
        "switch_no": "Okay, continuing with the {name}.",
        "back": "Back to the {name} — {step}.",
        "nav": "Please {step}.",
        "goto_list": "For this, open the Sugam services list and choose \"{name}\".",
        "ask_goal": "Which service do you need? Hold the mic button and tell me, e.g. \"I want an income certificate\".",
        "submit": "All fields are filled. Check everything once, then press \"{label}\" yourself.",
    },
}


_INDIC = "\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF"  # Devanagari (minus । ॥) + Gujarati
_JOIN_RE = re.compile(rf"(?<=[{_INDIC}])(?=[A-Za-z])|(?<=[A-Za-z])(?=[{_INDIC}])")  # letters only: keep "10वीं"


def space_scripts(text: str) -> str:
    """'केApplicant' → 'के Applicant': a space wherever an Indic run touches a Latin/digit run."""
    return _JOIN_RE.sub(" ", text)


_BACK_RE = re.compile(r"[^।.!?]*(चलिए,? वापस|back to the)[^।.!?]*[।.!?]?\s*", re.I)


def strip_back(text: str) -> str:
    """Drop a 'back to the goal' sentence the model wrote itself (the app appends its own)."""
    return _BACK_RE.sub("", text).strip() or text


def wav_seconds(wav: bytes) -> float:
    """Duration of a PCM WAV (header: byte rate at offset 28)."""
    if len(wav) < 44 or wav[:4] != b"RIFF":
        return 0.0
    byte_rate = int.from_bytes(wav[28:32], "little") or 1
    return max(0.0, (len(wav) - 44) / byte_rate)


def phrase(lang: str, key: str, **kw) -> str:
    return T.get(lang, T["en"])[key].format(**kw)


# Sensitive = password, OTP, Aadhaar NUMBER, PAN NUMBER, bank account/IFSC — typed inputs only.
# Never: mobile number, captcha, dropdowns/radios/checkboxes, file uploads, "Name as per Aadhaar".
SENSITIVE_RE = re.compile(
    r"password|पासवर्ड|પાસવર્ડ"
    r"|\botp\b|one.?time.?password|ओटीपी|ઓટીપી"
    r"|aadha?ar\s*(no\.?|num(ber)?|card\s*(no\.?|num(ber)?))|^\s*aadha?ar\s*$|आधार\s*(नंबर|संख्या|क्रमांक|न\.)|આધાર\s*(નંબર|ક્રમાંક)"
    r"|\bpan\s*(no\.?|num(ber)?|card\s*(no\.?|num(ber)?))|^\s*pan\s*$|पैन\s*(नंबर|संख्या)|પાન\s*(નંબર|ક્રમાંક)"
    r"|account\s*(no\.?|num(ber)?)|a/c\s*(no\.?|num(ber)?)|\bifsc\b|खाता\s*(नंबर|संख्या)|ખાતા\s*(નંબર|ક્રમાંક)",
    re.I,
)
NEVER_SENSITIVE_RE = re.compile(r"mobile|मोबाइल|મોબાઇલ|captcha|कैप्चा|કેપ્ચા", re.I)
TYPED_KINDS = {"text", "number", "password", "textarea", "other"}


def is_sensitive(f: SeenField) -> bool:
    if f.kind.value not in TYPED_KINDS or NEVER_SENSITIVE_RE.search(f.label):
        return False
    return f.kind.value == "password" or bool(SENSITIVE_RE.search(f.label))


def field_text(lang: str, f: SeenField) -> str:
    if is_sensitive(f):
        return phrase(lang, "sensitive", label=f.label)
    key = {"dropdown": "choose", "radio": "choose", "file": "upload", "checkbox": "tick"}.get(f.kind.value, "fill")
    text = phrase(lang, key, label=f.label)
    # Required/optional: say nothing unless the form map or an explicit on-page "(optional)" says so.
    # Never suggest skipping a field.
    mapped = form_field(f.label)
    optional = (mapped is not None and mapped.get("required") is False) or (mapped is None and f.marker.value == "optional")
    return text + (phrase(lang, "optional") if optional else "")


REGISTER_RE = re.compile(r"new registration|register|sign ?up|નોંધણી|पंजीकरण|रजिस्टर", re.I)
LOGIN_BTN_RE = re.compile(r"^\s*(citizen )?log ?in|sign ?in|login ?/ ?register|લૉગિન|લોગિન|लॉगिन", re.I)
REGISTRATION_QUESTION = "How do I register a new citizen account on Digital Gujarat? Give the first 2-3 steps."


CHECK_IN_POLL_S = 5.0
QUIET_AFTER_ANSWER_S = 5.0  # no field instructions / check-ins until this long after an answer's audio ends
NO_CHECKIN_AFTER_USER_S = 60.0


# ------------------------------------------------------------------ session
class Session:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.lang = "hi"
        self.active = False
        self.frame: bytes | None = None
        self.frame_seq = 0
        self.own_ui_visible = False
        self.frame_id: int | None = None  # client's id for the newest frame (echoed in highlights)
        self.analyzed_seq = 0
        self.timer: asyncio.TimerHandle | None = None
        self.last_big_change_seq = 0
        self.vision_task: asyncio.Task | None = None
        self.last_vision_t = 0.0
        self.force = False
        self.tracker = Tracker()
        self.scheme = SchemeDialog()
        self.answering = False
        self.quiet_until = 0.0
        self.quiet_seq = -1
        self.quiet_timer: asyncio.TimerHandle | None = None
        self.last_user_t = 0.0
        self.pending_checkin: str | None = None  # field key awaiting a fresh-frame confirmation
        self.turns: deque[dict] = deque(maxlen=16)  # last 8 exchanges {role, text}; memory only
        self.last_jpeg: bytes | None = None  # newest frame (for screen-aware answers); never stored to disk
        self.last_vision: VisionResult | None = None
        self.last_frame_id: int | None = None  # frame the latest detection came from
        self.checkin_task: asyncio.Task | None = None
        self.said_errors: set[str] = set()
        self.last_no_form_t = 0.0
        self.paused = False  # privacy pause: no frames while the user fills a sensitive field
        self.say_seq = 0
        self.closed = False
        self.bg: set[asyncio.Task] = set()  # keep refs to fire-and-forget tasks
        self.page_title: str | None = None
        self.has_account: bool | None = None  # answer to "Do you have a Digital Gujarat account?"
        self.pending_choice: str | None = None
        self.once: set[str] = set()  # one-time announcements already made (keyed by page)
        self.task = Task()  # goal + current step (persists across Start/Stop; cleared by Reset)

    async def send(self, type_: str, **data) -> None:
        if self.closed:
            return
        try:
            await self.ws.send_text(json.dumps({"type": type_, **data}, ensure_ascii=False))
        except (RuntimeError, WebSocketDisconnect):
            self.closed = True

    async def say(self, text: str, lang: str | None = None, answer: bool = False) -> None:
        """Caption now, audio when TTS returns — unless something newer was said meanwhile.
        answer=True: keep guidance quiet until QUIET_AFTER_ANSWER_S after this audio finishes."""
        text = space_scripts(text)
        self.say_seq += 1
        if answer:
            self.quiet_until = time.monotonic() + 30  # provisional until we know the audio length
            self.quiet_seq = self.say_seq  # this answer owns the quiet window
        await self.send("say", text=text, audio_b64=None)
        task = asyncio.create_task(self._speak(text, lang or self.lang, self.say_seq, answer))
        self.bg.add(task)
        task.add_done_callback(self.bg.discard)

    async def _speak(self, text: str, lang: str, seq: int, answer: bool = False) -> None:
        wav = await tts(text, lang)
        dur = wav_seconds(wav) if wav else 0.0
        if answer and seq == self.quiet_seq:  # always close the window this answer opened
            played = seq == self.say_seq and not self.closed
            self.end_quiet_at(time.monotonic() + (dur + QUIET_AFTER_ANSWER_S if played else 0.0))
        if wav and seq == self.say_seq and not self.closed:
            await self.send("say", text=text, audio_b64=base64.b64encode(wav).decode())

    # ---- guidance pause around Q&A
    def quiet(self) -> bool:
        return self.answering or time.monotonic() < self.quiet_until

    def end_quiet_at(self, t: float) -> None:
        self.quiet_until = t
        if self.quiet_timer:
            self.quiet_timer.cancel()
        self.quiet_timer = asyncio.get_running_loop().call_later(max(0.0, t - time.monotonic()) + 0.05, self._quiet_over)

    def _quiet_over(self) -> None:
        self.quiet_timer = None
        if not self.active or self.quiet():
            return
        if self.paused:  # capture is off, so no new frame will trigger the held-back private-field line
            t = self.tracker.current()
            if t and not t.explained:
                t.explained = True
                self.spawn(self.say(field_text(self.lang, t.seen)))
            return
        self.force = True  # fresh look; a held-back instruction is spoken only if that field is still on screen
        self.spawn(self.send("frame.request", reason="resume"))
        self.maybe_run_vision()

    # ---- client messages
    async def handle(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "start":
            self.lang = msg.get("lang") or self.lang
            self.reset()
            self.active = True
            self.force = True
            await self.send("state", value="watching")
            await self.send("capture", mode="on")
            await self.say(phrase(self.lang, "watching"))
        elif t == "stop":
            self.say_seq += 1  # drop audio still being synthesised
            self.active = False
            self.reset()
            await self.send("capture", mode="off")
            await self.send("clear")
            await self.send("state", value="idle")
        elif t == "reset":  # demo reset: forget everything this session learned (memory, schemes, tracker, ask-backs)
            self.say_seq += 1
            self.active = False
            self.reset()
            self.turns.clear()
            self.scheme = SchemeDialog()
            self.task = Task()
            self.has_account = None
            self.lang = msg.get("lang") or "hi"
            await self.send("capture", mode="off")
            await self.send("clear")
            await self.send("choice.clear")
            await self.send("state", value="idle")
            log.info("session reset")
        elif t == "lang":
            self.lang = msg.get("lang") or self.lang
            await self.explain_current()  # user asked for the other language → say it again in that one
        elif t == "next":
            self.tracker.next(time.monotonic())
            self.force = True
            await self.send("clear")
            if self.paused:  # user finished the private field → resume watching
                self.paused = False
                await self.send("capture", mode="on")
                await self.send("state", value="watching")
        elif t == "choice":
            await self.on_choice(msg.get("id"), msg.get("value"))
        elif t == "ask_text":  # typed question (pill text box / tests)
            self.spawn(self.handle_question(str(msg.get("text") or "")[:500], msg.get("lang") or self.lang))
        elif t == "ask":
            task = asyncio.create_task(self.handle_ask(msg))  # don't block frames while answering
            self.bg.add(task)
            task.add_done_callback(self.bg.discard)
        elif t == "frame" and self.active and not self.paused:
            try:
                self.frame = base64.b64decode(msg["jpeg_b64"])
            except (KeyError, ValueError):
                return
            self.frame_seq += 1
            self.last_jpeg = self.frame
            self.frame_id = msg.get("frame_id")
            self.own_ui_visible = bool(msg.get("own_ui_visible"))
            if msg.get("big_change"):
                self.last_big_change_seq = self.frame_seq
                self.force = True  # page moved: re-locate the same target (overlay already cleared itself)
        self.maybe_run_vision()

    async def handle_ask(self, msg: dict) -> None:
        try:
            wav = base64.b64decode(msg.get("audio_b64") or "")
        except ValueError:
            return
        self.say_seq += 1  # barge-in: drop any guidance audio still being synthesised
        self.answering = True  # hold guidance while we transcribe
        self.last_user_t = time.monotonic()
        await self.send("state", value="answering")
        heard = await stt(wav, msg.get("lang") or self.lang)
        self.answering = False
        if not heard:
            await self.say(phrase(self.lang, "not_heard"))
            return
        if heard.lang in T and heard.lang != self.lang:  # user switched language by speaking
            self.lang = heard.lang
            await self.send("lang", value=self.lang)
        await self.send("transcript", text=heard.text, lang=heard.lang, final=True)
        await self.handle_question(heard.text, self.lang)

    def remember(self, role: str, text: str) -> None:
        self.turns.append({"role": role, "text": text[:600]})

    def screen_context(self) -> ScreenContext:
        r = self.last_vision
        cur, done, rest = self.tracker.summary()
        ask_back = {} if self.has_account is None else {"has_digital_gujarat_account": self.has_account}
        return ScreenContext(
            page_type=r.page_type.value if r else None, page_title=(r.page_title or None) if r else None,
            logged_in=r.logged_in if r else None, current_field=cur, fields_done=done, fields_remaining=rest,
            ask_back=ask_back, service_id=self.task.goal or "gj_income_certificate", task=self.task.describe(),
        )

    async def handle_question(self, text: str, lang: str) -> None:
        if not text.strip():
            return
        self.last_user_t = time.monotonic()
        self.pending_checkin = None
        self.answering = True
        try:
            await self._handle_question(text, lang)
        finally:
            self.answering = False
            # answers set their own quiet window from the audio length; control/yes-no resume now — both via a fresh frame
            self.end_quiet_at(max(self.quiet_until, time.monotonic()))

    async def _handle_question(self, text: str, lang: str) -> None:
        r = route(text, pending_yes_no=self.pending_choice is not None, scheme_active=self.scheme.active)
        svc = find_service(text) if r == Route.answer else None
        if svc and (is_need(text) or (self.task.generic() and "caste" in svc[0] and svc[1] != self.task.goal_name)):
            r = Route.need  # stated need — or "unreserved wala" narrowing a generic caste goal
        log.info("route=%s", r.value)
        self.remember("user", text)
        if r in (Route.yes, Route.no):
            await self.on_choice(self.pending_choice, r.value)
            return
        if r == Route.need:
            await self.on_need(*svc)
            return
        if r == Route.next:
            await self.handle({"type": "next"})
            return
        if r == Route.repeat:
            await self.explain_current()
            return
        if r == Route.stop:
            await self.handle({"type": "stop"})
            await self.say(phrase(lang, "stopped"))
            return
        heard_text = text
        if r == Route.scheme:
            await self.send("state", value="answering")
            on_form = self.last_vision is not None and self.last_vision.page_type == PageType.application_form
            st = await scheme_step(self.scheme, text, lang, list(self.turns)[:-1], "gj_income_certificate" if on_form else None)
            if st is None:
                await self.say(phrase(self.lang, "answer_fail"))
                await self.send("state", value="guiding_field" if self.tracker.current() else ("watching" if self.active else "idle"))
                return
            if not st.exit:
                await self.say(st.say, answer=True)
                self.remember("assistant", st.say)
                if st.sources:
                    await self.send("sources", urls=st.sources)
                if st.link:
                    await self.send("link", url=st.link["url"], title=st.link["title"])
                await self.send("state", value="scheme")
                return
        await self.send("state", value="answering")
        a = await answer(heard_text, lang, self.screen_context(), self.last_jpeg, list(self.turns)[:-1])
        if not a:
            await self.say(phrase(self.lang, "answer_fail"))
            await self.send("state", value="guiding_field" if self.tracker.current() else ("watching" if self.active else "idle"))
            return
        self.task.last_topic = a.topic or None
        said = strip_back(a.say)
        text = said
        if self.task.goal and not a.about_goal and (back := self.back_line()):  # side question → pop back to the goal
            text = f"{said} {back}"
        await self.say(text, answer=True)
        self.remember("assistant", said)  # without the back line, so the model never learns to copy it
        if a.sources:
            await self.send("sources", urls=a.sources[:3])
        await self.send("state", value="guiding_field" if self.tracker.current() else ("watching" if self.active else "idle"))

    def back_line(self) -> str:
        step = self.task.step_text(self.lang)
        return phrase(self.lang, "back", name=self.task.goal_name, step=step) if step else ""

    async def on_need(self, sid: str, name: str) -> None:
        """User stated a need for a covered service: set the goal, or confirm a switch (never switch silently)."""
        if not self.task.goal:
            self.task.set_goal(sid, name)
            await self.say(phrase(self.lang, "goal_set", name=name))
            self.remember("assistant", phrase(self.lang, "goal_set", name=name))
            self.once = {k for k in self.once if not k.startswith("nav:")}  # speak the first step on this screen
            self.force = True
            return
        if self.task.generic() and "caste" in sid and name != self.task.goal_name:  # "caste" → "unreserved caste": refine, no question
            left = self.task.left_form
            self.task.set_goal(sid, name)
            self.task.left_form = left
            await self.say(phrase(self.lang, "switch_yes", name=name))
            self.remember("assistant", phrase(self.lang, "switch_yes", name=name))
            self.once = {k for k in self.once if not k.startswith("nav:")}
            self.force = True
            return
        if sid == self.task.goal or (self.task.same_family(sid) and "caste" not in sid):
            line = phrase(self.lang, "goal_same", name=self.task.goal_name)
            if (step := self.task.step_text(self.lang)):
                line = f"{line} {phrase(self.lang, 'nav', step=step)}"
            await self.say(line)
            self.remember("assistant", line)
            return
        self.task.pending_switch = (sid, name)
        self.pending_choice = "switch_goal"
        q = phrase(self.lang, "switch_q", old=self.task.goal_name, new=name)
        await self.send("choice", id="switch_goal", question=q,
                        options=[{"id": "yes", "label": phrase(self.lang, "yes")}, {"id": "no", "label": phrase(self.lang, "no")}])
        await self.say(q)
        self.remember("assistant", q)

    async def on_choice(self, cid: str | None, value: str | None) -> None:
        if cid == "switch_goal" and value in ("yes", "no") and self.task.pending_switch:
            sid, name = self.task.pending_switch
            self.pending_choice = None
            self.task.pending_switch = None
            await self.send("choice.clear")
            if value == "yes":
                self.task.set_goal(sid, name)
                self.tracker = Tracker()  # the old form's progress no longer applies
                self.once = {k for k in self.once if not k.startswith(("nav:", "submit:"))}
                on_form = self.last_vision is not None and self.last_vision.page_type == PageType.application_form
                line = phrase(self.lang, "switch_yes", name=name)
                if on_form:  # this is the old service's form: the new one starts from the services list
                    self.task.left_form = (self.last_vision.page_title or "").strip().lower()
                    line = f"{line} {phrase(self.lang, 'goto_list', name=name)}"
                    await self.send("clear")
                await self.say(line)
            else:
                line = phrase(self.lang, "switch_no", name=self.task.goal_name)
                if (back := self.task.step_text(self.lang)):
                    line = f"{line} {phrase(self.lang, 'nav', step=back)}"
                await self.say(line)
            self.remember("assistant", line)
            self.force = True
            self.maybe_run_vision()
            return
        if cid != "has_account" or value not in ("yes", "no"):
            return
        self.pending_choice = None
        self.has_account = value == "yes"
        await self.send("choice.clear")
        if self.has_account:
            await self.say(phrase(self.lang, "login_fields"))
        self.force = True
        self.maybe_run_vision()

    async def say_once(self, key: str, text: str) -> bool:
        if key in self.once:
            return False
        self.once.add(key)
        await self.say(text)
        return True

    def find_action(self, r: VisionResult, rx: re.Pattern):
        return next((a for a in r.actions if rx.search(a.text)), None)

    async def explain_registration(self) -> None:
        a = await answer(REGISTRATION_QUESTION, self.lang, self.screen_context(), self.last_jpeg, list(self.turns))
        if a and a.say:
            await self.say(a.say)
            self.remember("assistant", a.say)
            if a.sources:
                await self.send("sources", urls=a.sources[:3])

    async def page_gate(self, r: VisionResult, frame_id: int | None) -> bool:
        """Page-type behaviour before the field loop. Returns True if it handled this frame."""
        pt = r.page_type
        page = f"{pt.value}|{(r.page_title or '').strip().lower()}"
        if pt != PageType.login and self.pending_choice == "has_account":  # the question belonged to the login page
            self.pending_choice = None
            await self.send("choice.clear")
        nav = self.task.nav_step(r)  # card → Apply Online on non-form pages: highlight, say once, wait for the screen to change
        if nav:
            self.task.step = nav
            if nav.box:
                await self.send("highlight", frame_id=frame_id, bbox=nav.box, label=nav.label)
            else:
                await self.send("clear")
            await self.send("state", value="guiding_step")
            await self.say_once(f"nav:{page}:{nav.kind}:{nav.label.strip().lower()}",
                                phrase(self.lang, "nav", step=self.task.step_text(self.lang)))
            return True
        if pt == PageType.application_form and self.task.left_form is not None \
                and (r.page_title or "").strip().lower() == self.task.left_form:
            await self.send("clear")  # the form of the goal the user just left: don't guide it
            return True
        if pt == PageType.login:
            self.task.step = Step("login")
            if self.has_account is None:
                await self.send("clear")
                if self.pending_choice != "has_account":
                    self.pending_choice = "has_account"
                    await self.send("choice", id="has_account", question=phrase(self.lang, "has_account"),
                                    options=[{"id": "yes", "label": phrase(self.lang, "yes")},
                                             {"id": "no", "label": phrase(self.lang, "no")}])
                    await self.say(phrase(self.lang, "has_account"))
                    self.remember("assistant", phrase(self.lang, "has_account"))
                return True
            if self.has_account is False:
                btn = self.find_action(r, REGISTER_RE)
                if btn:
                    await self.send("highlight", frame_id=frame_id, bbox=btn.box_2d, label=btn.text)
                    if await self.say_once("register:" + page, phrase(self.lang, "register_btn", label=btn.text)):
                        self.spawn(self.explain_registration())
                else:
                    await self.send("clear")
                    await self.say_once("register_find:" + page, phrase(self.lang, "register_find"))
                return True
            return False  # has account → guide the login fields
        if pt == PageType.service_list and not self.task.goal:
            await self.send("clear")
            await self.say_once("ask_goal", phrase(self.lang, "ask_goal"))
            return True
        if not r.logged_in and (pt == PageType.service_list or (pt == PageType.application_form and self.find_action(r, LOGIN_BTN_RE))):
            btn = self.find_action(r, LOGIN_BTN_RE)
            if btn:
                await self.send("highlight", frame_id=frame_id, bbox=btn.box_2d, label=btn.text)
            await self.say_once("login_first:" + page, phrase(self.lang, "login_first"))
            return True
        if pt == PageType.acknowledgement:
            await self.send("clear")
            await self.say_once("ack:" + page, phrase(self.lang, "ack"))
            return True
        if pt == PageType.payment:
            await self.say_once("payment:" + page, phrase(self.lang, "payment"))
            return False  # still guide any payment-method choices
        return False

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.bg.add(task)
        task.add_done_callback(self.bg.discard)

    def reset(self) -> None:
        self.paused = False
        self.pending_checkin = None
        self.quiet_until = 0.0
        self.quiet_seq = -1
        self.answering = False
        if self.quiet_timer:
            self.quiet_timer.cancel()
            self.quiet_timer = None
        if self.checkin_task and not self.checkin_task.done():
            self.checkin_task.cancel()
        self.checkin_task = None
        if self.vision_task and not self.vision_task.done():
            self.vision_task.cancel()
        self.vision_task = None
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.frame = None
        self.last_jpeg = None
        self.last_vision = None
        self.tracker = Tracker()
        self.said_errors.clear()
        self.last_no_form_t = 0.0
        self.pending_choice = None
        self.once.clear()

    # ---- vision loop
    def maybe_run_vision(self) -> None:
        """Analyse the newest frame if it's unseen (or forced), at most once per VISION_MIN_INTERVAL_S."""
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if not self.active or self.frame is None:
            return
        if self.vision_task and not self.vision_task.done():
            return  # run_vision re-checks when it finishes
        if self.quiet():
            return  # answer mode: highlights + field speech frozen; _quiet_over resumes with a fresh frame
        if self.frame_seq <= self.analyzed_seq and not self.force:
            return
        since = time.monotonic() - self.last_vision_t
        wait = (1.0 if self.force else VISION_MIN_INTERVAL_S) - since
        if wait > 0:
            self.timer = asyncio.get_running_loop().call_later(wait + 0.02, self.maybe_run_vision)
            return
        self.force = False
        self.last_vision_t = time.monotonic()
        self.analyzed_seq = self.frame_seq
        self.vision_task = asyncio.create_task(self.run_vision(self.frame, self.frame_seq, self.frame_id))

    async def run_vision(self, jpeg: bytes, seq: int, frame_id: int | None) -> None:
        try:
            await self._run_vision(jpeg, seq, frame_id)
        finally:
            if self.vision_task is asyncio.current_task():
                self.vision_task = None  # else maybe_run_vision sees this task as still running
            if self.active:
                self.maybe_run_vision()

    async def _run_vision(self, jpeg: bytes, seq: int, frame_id: int | None) -> None:
        result = await detect(jpeg, own_ui_visible=self.own_ui_visible, goal=self.task.goal_name or None)
        if not self.active:
            return
        if self.quiet():  # a question started while this frame was being read: drop, re-read after the answer
            self.force = True
            return
        if result is None:
            await self.send("state", value="vision_retry")
            self.force = True  # retry the same frame (after the 1 s force cooldown)
            self.analyzed_seq = min(self.analyzed_seq, seq - 1)
            return
        if self.last_big_change_seq > seq:
            log.info("vision result stale (scrolled), dropping")
            self.force = True
            return
        await self.apply(result, frame_id)

    async def apply(self, r: VisionResult, frame_id: int | None = None) -> None:
        self.page_title = r.page_title or None
        self.last_vision = r
        if await self.page_gate(r, frame_id):
            return
        if not r.is_form or not r.fields:
            await self.send("clear")
            if time.monotonic() - self.last_no_form_t > NO_FORM_REPEAT_S:
                self.last_no_form_t = time.monotonic()
                await self.say(phrase(self.lang, "no_form"))
            return

        self.last_frame_id = frame_id
        d = self.tracker.update(r, time.monotonic())

        for e in r.errors:  # new validation message → point at it once; the target stays the same
            k = e.text.strip().lower()
            if k and k not in self.said_errors:
                self.said_errors.add(k)
                await self.send("highlight", frame_id=frame_id, bbox=e.box_2d, label="⚠ " + e.text[:80])
                await self.say(phrase(self.lang, "error", text=e.text))
                return

        if d.target is None:
            btn = next((a for a in r.actions if SUBMIT_RE.search(a.text)), None)
            if btn and (d.all_done or self.task.step.kind == "submit"):  # pre-submit: point at the button, user presses it
                self.task.step = Step("submit", btn.text, btn.box_2d)
                await self.send("highlight", frame_id=frame_id, bbox=btn.box_2d, label=btn.text)
                if not self.quiet():
                    await self.say_once(f"submit:{r.page_title}:{btn.text}", phrase(self.lang, "submit", label=btn.text))
                return
            await self.send("clear")
            if d.all_done:
                if self.quiet():
                    self.tracker.page.all_done_said = False  # say it after the pause instead
                else:
                    await self.say(phrase(self.lang, "done_visible"))
            return

        t = d.target
        self.task.step = Step("field", t.seen.label, t.bbox)
        if t.bbox:
            await self.send("highlight", frame_id=frame_id, bbox=t.bbox, label=t.seen.label)  # position may shift
        else:
            await self.send("clear")  # target scrolled out of view; keep it as target
        if is_sensitive(t.seen) and t.bbox:
            # Privacy pause (PLAYBOOK §16): stop capture before the user types Aadhaar/OTP/password/bank data.
            # The box stays; Next resumes capture and moves on.
            self.paused = True
            await self.send("redact", frame_id=frame_id, bbox=t.bbox)  # client blanks it in later frames
            self.frame = None
            await self.send("capture", mode="paused")
            await self.send("state", value="paused_secure")
            if d.explain:
                if self.quiet():
                    t.explained = False  # _quiet_over speaks it
                else:
                    await self.say(field_text(self.lang, t.seen))
            return
        if d.explain:
            if self.quiet() or not t.bbox:
                t.explained = False  # held: answering, or the field isn't on screen right now
            else:
                await self.send("state", value="guiding_field")
                await self.say(field_text(self.lang, t.seen))
        # check-in confirmed on this fresh frame: same target, still on screen, still empty
        if self.pending_checkin:
            key, self.pending_checkin = self.pending_checkin, None
            if t.key == key and t.bbox and not t.filled_now and not self.quiet() and not self.user_spoke_recently():
                await self.say(phrase(self.lang, "check_in", label=t.seen.label))
        self.ensure_checkin_loop()

    def user_spoke_recently(self) -> bool:
        return time.monotonic() - self.last_user_t < NO_CHECKIN_AFTER_USER_S

    async def explain_current(self) -> None:
        t = self.tracker.current()
        if t:
            await self.say(field_text(self.lang, t.seen))

    def ensure_checkin_loop(self) -> None:
        if not self.checkin_task or self.checkin_task.done():
            self.checkin_task = asyncio.create_task(self._checkin_loop())

    async def _checkin_loop(self) -> None:
        """No progress for 30 s on the same field → one short check-in (per field)."""
        while self.active and not self.closed:
            await asyncio.sleep(CHECK_IN_POLL_S)
            if self.paused or not self.active or self.quiet() or self.user_spoke_recently() or self.pending_checkin:
                continue
            t = self.tracker.check_in_due(time.monotonic())  # consumes this field's single check-in
            if t:
                # never remind from a stale view: ask for a fresh frame, re-detect, speak only if still true
                self.pending_checkin = t.key
                await self.send("frame.request", reason="check_in")
                self.force = True
                self.maybe_run_vision()


@app.websocket("/ws/session")
async def ws_session(ws: WebSocket) -> None:
    await ws.accept()
    s = Session(ws)
    log.info("session open")
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await s.handle(msg)
    except WebSocketDisconnect:
        pass
    finally:
        s.active = False
        s.closed = True
        s.reset()
        log.info("session closed")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=env_int("BACKEND_PORT", 8765))
