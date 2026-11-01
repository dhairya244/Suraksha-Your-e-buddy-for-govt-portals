"""Replay a screenshot + typed questions against a running backend and print what Suraksha says.

  .venv/bin/python -m backend.server &            # or run/start-mac.sh
  .venv/bin/python scripts/qa_check.py --image ~/Desktop/form.png
  .venv/bin/python scripts/qa_check.py --image form.png --lang hi --q "apply kaise karu" --q "aur koi?"

Default questions = the live-test set. The screenshot is sent like a desktop frame (in memory only).
Crop it to the browser window for realistic results.

Scenario mode (one session, steps in order) — replays a whole conversation across screens:
  .venv/bin/python scripts/qa_check.py --step img=list.png --step "q=I want to create my income certificate" \
      --step img=service.png --step img=login.png --step choice=yes --step img=form.png --step "q=driving license banana hai"
  img=PATH   send a new screenshot (as a page change) and wait for guidance to settle
  q=TEXT     ask (typed, like the pill text box / speech after STT)
  choice=yes|no  press the pill's choice button
  wait=SEC   just listen
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import time
from pathlib import Path

import websockets

DEFAULT_QS = [
    "apply kaise karu",
    "is page par kya bharna hai",
    "kaun se documents chahiye",
    "mere liye kaun si yojana hai",
    "main student hoon",
    "aur koi?",
    "iske documents?",
]


def jpeg_b64(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix.lower() in (".jpg", ".jpeg"):
        return base64.b64encode(data).decode()
    try:
        from PIL import Image  # optional
        buf = io.BytesIO()
        Image.open(io.BytesIO(data)).convert("RGB").save(buf, "JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except ImportError:
        import subprocess, tempfile  # macOS fallback
        out = Path(tempfile.mkdtemp()) / "f.jpg"
        subprocess.run(["sips", "-s", "format", "jpeg", str(path), "--out", str(out)], check=True, capture_output=True)
        return base64.b64encode(out.read_bytes()).decode()


async def scenario(a: argparse.Namespace) -> None:
    async with websockets.connect(f"ws://127.0.0.1:{a.port}/ws/session", max_size=None) as ws:
        t0 = time.time()
        fid = 0
        pending_choice = {}

        def show(m: dict) -> None:
            if m["type"] == "say" and not m.get("audio_b64"):
                print(f"   {time.time() - t0:5.1f}s 🔊 {m['text']}")
            elif m["type"] == "highlight":
                print(f"   {time.time() - t0:5.1f}s [highlight] {m.get('label')}")
            elif m["type"] == "choice":
                pending_choice["id"] = m["id"]
                print(f"   {time.time() - t0:5.1f}s [choice] {m.get('question')} {[o['label'] for o in m['options']]}")
            elif m["type"] in ("sources", "link", "choice.clear", "clear"):
                print(f"   {time.time() - t0:5.1f}s [{m['type']}] {m.get('urls') or m.get('url') or ''}")

        async def drain(seconds: float, until=None) -> None:
            end = time.time() + seconds
            while time.time() < end:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), end - time.time()))
                except asyncio.TimeoutError:
                    return
                show(m)
                if until and until(m):
                    return

        await ws.send(json.dumps({"type": "start", "lang": a.lang}))
        await drain(1.0)
        for st in a.step:
            kind, _, val = st.partition("=")
            print(f"\n== {kind}: {val}")
            t0 = time.time()
            if kind == "img":
                fid += 1
                await ws.send(json.dumps({"type": "frame", "frame_id": fid, "jpeg_b64": jpeg_b64(Path(val)), "w": 0, "h": 0, "big_change": True}))
                await drain(a.settle)
            elif kind == "q":
                await ws.send(json.dumps({"type": "ask_text", "text": val, "lang": a.lang}))
                await drain(a.settle + 10)  # answer + its quiet window + the resume on a fresh frame
            elif kind == "choice":
                await ws.send(json.dumps({"type": "choice", "id": pending_choice.get("id", "has_account"), "value": val}))
                await drain(a.settle)
            elif kind == "wait":
                await drain(float(val))


async def main(a: argparse.Namespace) -> None:
    if a.step:
        return await scenario(a)
    qs = a.q or DEFAULT_QS
    async with websockets.connect(f"ws://127.0.0.1:{a.port}/ws/session", max_size=None) as ws:
        t0 = time.time()

        def show(m: dict) -> None:
            if m["type"] == "say" and not m.get("audio_b64"):
                print(f"   {time.time() - t0:5.1f}s 🔊 {m['text']}")
            elif m["type"] in ("highlight", "choice", "sources", "link"):
                print(f"   {time.time() - t0:5.1f}s [{m['type']}] {m.get('label') or m.get('question') or m.get('urls') or m.get('url')}")

        async def drain(seconds: float, until=None) -> None:
            end = time.time() + seconds
            while time.time() < end:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), end - time.time()))
                except asyncio.TimeoutError:
                    return
                show(m)
                if until and until(m):
                    return

        await ws.send(json.dumps({"type": "start", "lang": a.lang}))
        await ws.send(json.dumps({"type": "frame", "frame_id": 1, "jpeg_b64": jpeg_b64(a.image), "w": 0, "h": 0}))
        print("== guidance on the screenshot")
        await drain(a.settle)
        for q in qs:
            print(f"\n== Q: {q}")
            t0 = time.time()
            await ws.send(json.dumps({"type": "ask_text", "text": q, "lang": a.lang}))
            await drain(30, until=lambda m: m["type"] == "state" and m.get("value") in ("guiding_field", "watching", "idle", "scheme"))
            await drain(1.5)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", type=Path)
    ap.add_argument("--step", action="append", help="scenario step: img=PATH | q=TEXT | choice=yes|no | wait=SEC")
    ap.add_argument("--q", action="append", help="question (repeatable); default = live-test set")
    ap.add_argument("--lang", default="hi")
    ap.add_argument("--port", default="8765")
    ap.add_argument("--settle", type=float, default=12.0, help="seconds to let vision run before asking")
    asyncio.run(main(ap.parse_args()))
