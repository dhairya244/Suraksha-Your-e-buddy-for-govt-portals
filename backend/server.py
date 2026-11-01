"""Suraksha backend: WebSocket session that turns screen frames into field highlights (M2).

  .venv/bin/uvicorn backend.server:app --port 8765
  (or) .venv/bin/python -m backend.server

Protocol (matches desktop/src/main.ts):
  client → {type:"start", lang} | {type:"frame", frame_id, jpeg_b64, w, h, big_change, browser} | {type:"next"}
           | {type:"stop"} | {type:"lang", lang} | {type:"ask", audio_b64, lang} | {type:"choice", id, value}
  server → {type:"capture", mode} | {type:"highlight", frame_id, bbox, label} | {type:"clear"}
           | {type:"redact", frame_id, bbox} | {type:"transcript", text, lang, final}
           | {type:"lang", value} | {type:"sources", urls}
           | {type:"choice", id, question, options:[{id,label}]} | {type:"choice.clear"}
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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from backend.config import env_int
from backend.vision import PageType, SeenField, VisionResult, detect
from backend.answer import answer
from backend.knowledge import form_field
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
        "has_account": "क्या आपका Digital Gujarat अकाउंट है?",
        "yes": "हाँ", "no": "नहीं",
        "login_fields": "ठीक है, लॉगिन करते हैं।",
        "register_btn": "आपका अकाउंट नहीं है तो पहले रजिस्टर करना होगा। \"{label}\" बटन दबाइए।",
        "register_find": "आपका अकाउंट नहीं है तो पहले रजिस्टर करना होगा। पेज पर \"New Registration (Citizen)\" ढूँढिए।",
        "login_first": "यह सेवा इस्तेमाल करने के लिए पहले Digital Gujarat पर लॉगिन कीजिए।",
        "payment": "यह पेमेंट पेज है। भुगतान का तरीका चुनिए — Pay बटन आप खुद दबाइए।",
        "ack": "आवेदन जमा हो गया लगता है। आवेदन नंबर लिख लीजिए या रसीद डाउनलोड कर लीजिए।",
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
        "has_account": "Do you have a Digital Gujarat account?",
        "yes": "Yes", "no": "No",
        "login_fields": "Okay, let's log in.",
        "register_btn": "Without an account you need to register first. Press the \"{label}\" button.",
        "register_find": "Without an account you need to register first. Look for \"New Registration (Citizen)\" on the page.",
        "login_first": "Please log in to Digital Gujarat first to use this service.",
        "payment": "This is the payment page. Choose a payment method — you press Pay yourself.",
        "ack": "Your application looks submitted. Note down the application number or download the receipt.",
    },
}


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
YES_RE = re.compile(r"\b(haan|han|haa|ha|yes|ji|jee)\b|हाँ|हां|जी|હા", re.I)
HAVE_RE = re.compile(r"\bhai\b|है|છે|\bhave\b", re.I)  # "account hai" = yes, unless a "no" word is present
NO_RE = re.compile(r"\b(nahi|nahin|nai|no|nope|na)\b|नहीं|नही|(?<!\S)ना(?!\S)|ના|નથી", re.I)
REGISTRATION_QUESTION = "How do I register a new citizen account on Digital Gujarat? Give the first 2-3 steps."


def field_key(f: SeenField) -> str:
    return " ".join(f.label.lower().split())


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
        self.skipped: set[str] = set()
        self.target: str | None = None  # field_key of the highlighted field
        self.said_errors: set[str] = set()
        self.last_no_form_t = 0.0
        self.done_said = False
        self.paused = False  # privacy pause: no frames while the user fills a sensitive field
        self.say_seq = 0
        self.closed = False
        self.bg: set[asyncio.Task] = set()  # keep refs to fire-and-forget tasks
        self.page_title: str | None = None
        self.target_label: str | None = None
        self.has_account: bool | None = None  # answer to "Do you have a Digital Gujarat account?"
        self.pending_choice: str | None = None
        self.once: set[str] = set()  # one-time announcements already made (keyed by page)

    async def send(self, type_: str, **data) -> None:
        if self.closed:
            return
        try:
            await self.ws.send_text(json.dumps({"type": type_, **data}, ensure_ascii=False))
        except (RuntimeError, WebSocketDisconnect):
            self.closed = True

    async def say(self, text: str, lang: str | None = None) -> None:
        """Caption now, audio when TTS returns — unless something newer was said meanwhile."""
        self.say_seq += 1
        await self.send("say", text=text, audio_b64=None)
        task = asyncio.create_task(self._speak(text, lang or self.lang, self.say_seq))
        self.bg.add(task)
        task.add_done_callback(self.bg.discard)

    async def _speak(self, text: str, lang: str, seq: int) -> None:
        wav = await tts(text, lang)
        if wav and seq == self.say_seq and not self.closed:
            await self.send("say", text=text, audio_b64=base64.b64encode(wav).decode())

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
        elif t == "lang":
            self.lang = msg.get("lang") or self.lang
            self.target = None  # re-announce current field in the new language
            self.force = True
        elif t == "next":
            if self.target:
                self.skipped.add(self.target)
            self.target = None
            self.force = True
            await self.send("clear")
            if self.paused:  # user finished the private field → resume watching
                self.paused = False
                await self.send("capture", mode="on")
                await self.send("state", value="watching")
        elif t == "choice":
            await self.on_choice(msg.get("id"), msg.get("value"))
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
            self.frame_id = msg.get("frame_id")
            self.own_ui_visible = bool(msg.get("own_ui_visible"))
            if msg.get("big_change"):
                self.last_big_change_seq = self.frame_seq
                self.target = None  # page moved: re-highlight (overlay already cleared itself)
                self.force = True
        self.maybe_run_vision()

    async def handle_ask(self, msg: dict) -> None:
        try:
            wav = base64.b64decode(msg.get("audio_b64") or "")
        except ValueError:
            return
        self.say_seq += 1  # barge-in: drop any guidance audio still being synthesised
        await self.send("state", value="answering")
        heard = await stt(wav, msg.get("lang") or self.lang)
        if not heard:
            await self.say(phrase(self.lang, "not_heard"))
            return
        if heard.lang in T and heard.lang != self.lang:  # user switched language by speaking
            self.lang = heard.lang
            await self.send("lang", value=self.lang)
        await self.send("transcript", text=heard.text, lang=heard.lang, final=True)
        if self.pending_choice == "has_account":  # spoken Haan / Nahi
            no = bool(NO_RE.search(heard.text))
            yes = bool(YES_RE.search(heard.text)) or (not no and bool(HAVE_RE.search(heard.text)))
            if yes != no:
                await self.on_choice("has_account", "yes" if yes else "no")
                return
        a = await answer(heard.text, self.lang, self.page_title, self.target_label)
        if not a:
            await self.say(phrase(self.lang, "answer_fail"))
            return
        await self.say(a.say)
        if a.sources:
            await self.send("sources", urls=a.sources[:3])
        await self.send("state", value="guiding_field" if self.target else ("watching" if self.active else "idle"))

    async def on_choice(self, cid: str | None, value: str | None) -> None:
        if cid != "has_account" or value not in ("yes", "no"):
            return
        self.pending_choice = None
        self.has_account = value == "yes"
        await self.send("choice.clear")
        if self.has_account:
            await self.say(phrase(self.lang, "login_fields"))
        self.target = None
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
        a = await answer(REGISTRATION_QUESTION, self.lang, self.page_title, None)
        if a and a.say:
            await self.say(a.say)
            if a.sources:
                await self.send("sources", urls=a.sources[:3])

    async def page_gate(self, r: VisionResult, frame_id: int | None) -> bool:
        """Page-type behaviour before the field loop. Returns True if it handled this frame."""
        pt = r.page_type
        page = f"{pt.value}|{(r.page_title or '').strip().lower()}"
        if pt == PageType.login:
            if self.has_account is None:
                await self.send("clear")
                if self.pending_choice != "has_account":
                    self.pending_choice = "has_account"
                    await self.send("choice", id="has_account", question=phrase(self.lang, "has_account"),
                                    options=[{"id": "yes", "label": phrase(self.lang, "yes")},
                                             {"id": "no", "label": phrase(self.lang, "no")}])
                    await self.say(phrase(self.lang, "has_account"))
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
        if self.vision_task and not self.vision_task.done():
            self.vision_task.cancel()
        self.vision_task = None
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.frame = None
        self.skipped.clear()
        self.target = None
        self.said_errors.clear()
        self.done_said = False
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
        result = await detect(jpeg, own_ui_visible=self.own_ui_visible)
        if not self.active:
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
        if await self.page_gate(r, frame_id):
            return
        if not r.is_form or not r.fields:
            self.target = None
            await self.send("clear")
            if time.monotonic() - self.last_no_form_t > NO_FORM_REPEAT_S:
                self.last_no_form_t = time.monotonic()
                await self.say(phrase(self.lang, "no_form"))
            return

        for e in r.errors:
            k = e.text.strip().lower()
            if k and k not in self.said_errors:
                self.said_errors.add(k)
                await self.send("highlight", frame_id=frame_id, bbox=e.box_2d, label="⚠ " + e.text[:80])
                await self.say(phrase(self.lang, "error", text=e.text))
                self.target = None
                return

        todo = [f for f in r.fields if not f.filled and field_key(f) not in self.skipped]
        if not todo:
            self.target = None
            await self.send("clear")
            if not self.done_said:
                self.done_said = True
                await self.say(phrase(self.lang, "done_visible"))
            return

        self.done_said = False
        f = todo[0]
        k = field_key(f)
        self.target_label = f.label
        await self.send("highlight", frame_id=frame_id, bbox=f.box_2d, label=f.label)  # re-send: position may shift
        if is_sensitive(f):
            # Privacy pause (PLAYBOOK §16): stop capture before the user types Aadhaar/OTP/password/bank data.
            # The box stays; Next resumes capture and moves on.
            self.target = k
            self.paused = True
            await self.send("redact", frame_id=frame_id, bbox=f.box_2d)  # client blanks it in later frames
            self.frame = None
            await self.send("capture", mode="paused")
            await self.send("state", value="paused_secure")
            await self.say(field_text(self.lang, f))
            return
        if k != self.target:
            self.target = k
            await self.send("state", value="guiding_field")
            await self.say(field_text(self.lang, f))


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
