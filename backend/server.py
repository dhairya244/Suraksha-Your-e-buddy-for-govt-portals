"""Suraksha backend: WebSocket session that turns screen frames into field highlights (M2).

  .venv/bin/uvicorn backend.server:app --port 8765
  (or) .venv/bin/python -m backend.server

Protocol (matches desktop/src/main.ts):
  client → {type:"start", lang} | {type:"frame", frame_id, jpeg_b64, w, h, big_change, browser} | {type:"next"}
           | {type:"stop"} | {type:"lang", lang} | {type:"ask", audio_b64, lang}
  server → {type:"capture", mode} | {type:"highlight", frame_id, bbox, label} | {type:"clear"}
           | {type:"say", text, audio_b64?} | {type:"state", value}
bbox = [ymin, xmin, ymax, xmax] 0–1000 relative to the frame (the client maps it via that frame's crop).
Frames and audio are processed in memory only; nothing is written to disk or logged.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from backend.config import env_int
from backend.vision import SeenField, VisionResult, detect

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
        "watching": "Theek hai, main screen dekh raha hoon. Form wala page kholiye.",
        "no_form": "Mujhe abhi koi form nahi dikh raha. Form wala page kholiye.",
        "fill": "Yahan \"{label}\" bhariye.",
        "choose": "Yahan \"{label}\" mein se sahi vikalp chuniye.",
        "upload": "Yahan \"{label}\" ke liye document upload kariye.",
        "tick": "\"{label}\" — agar laagu ho to tick kariye.",
        "sensitive": "\"{label}\" private jaankari hai — ise khud bhariye. Ho jaaye to Next dabaiye.",
        "optional": " (Yeh zaroori nahi hai — chhodna ho to Next dabaiye.)",
        "done_visible": "Is hisse ke saare khaane bhar gaye. Neeche scroll kariye ya agla button dabaiye.",
        "error": "Dhyaan dijiye, form par galti dikh rahi hai: \"{text}\"",
        "stopped": "Ruk gaya.",
        "ask_todo": "Sawal-jawab jald aa raha hai. Abhi Next dabakar aage badhiye.",
        "vision_fail": "Screen padhne mein dikkat hui, dobara koshish kar raha hoon…",
    },
    "en": {
        "watching": "Okay, I'm watching the screen. Please open the form page.",
        "no_form": "I can't see a form right now. Please open the form page.",
        "fill": "Fill in \"{label}\" here.",
        "choose": "Choose the right option for \"{label}\" here.",
        "upload": "Upload the document for \"{label}\" here.",
        "tick": "\"{label}\" — tick this if it applies.",
        "sensitive": "\"{label}\" is private — please fill it yourself. Press Next when done.",
        "optional": " (Optional — press Next to skip.)",
        "done_visible": "Everything visible here is filled. Scroll down or press the next button on the page.",
        "error": "Heads up, the form shows an error: \"{text}\"",
        "stopped": "Stopped.",
        "ask_todo": "Questions are coming soon. For now, press Next to continue.",
        "vision_fail": "Had trouble reading the screen, retrying…",
    },
}


def phrase(lang: str, key: str, **kw) -> str:
    return T.get(lang, T["en"])[key].format(**kw)


def field_text(lang: str, f: SeenField) -> str:
    if f.sensitive:
        return phrase(lang, "sensitive", label=f.label)
    key = {"dropdown": "choose", "radio": "choose", "file": "upload", "checkbox": "tick"}.get(f.kind.value, "fill")
    text = phrase(lang, key, label=f.label)
    return text + (phrase(lang, "optional") if not f.required else "")


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

    async def send(self, type_: str, **data) -> None:
        await self.ws.send_text(json.dumps({"type": type_, **data}, ensure_ascii=False))

    async def say(self, text: str) -> None:
        await self.send("say", text=text, audio_b64=None)

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
        elif t == "ask":
            await self.say(phrase(msg.get("lang") or self.lang, "ask_todo"))
        elif t == "frame" and self.active:
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

    def reset(self) -> None:
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
            return
        if self.last_big_change_seq > seq:
            log.info("vision result stale (scrolled), dropping")
            self.force = True
            return
        await self.apply(result, frame_id)

    async def apply(self, r: VisionResult, frame_id: int | None = None) -> None:
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
        await self.send("highlight", frame_id=frame_id, bbox=f.box_2d, label=f.label)  # re-send: position may shift
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
        s.reset()
        log.info("session closed")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=env_int("BACKEND_PORT", 8765))
