// Suraksha desktop main process: overlay + pill windows, screen capture, WebSocket to backend.
// Never logs screenshots, transcripts or audio.
import { app, BrowserWindow, desktopCapturer, ipcMain, nativeImage, screen, session, shell, systemPreferences } from "electron";
import path from "path";
import WebSocket from "ws";
import { activeWindow, isBrowser, openWindows, Rect } from "./windows";

const PORT = process.env.BACKEND_PORT || "8765";
const WS_URL = `ws://127.0.0.1:${PORT}/ws/session`;
const CAPTURE_MS = 1500;
const CELL_DIFF = 10; // grayscale delta (0-255) for a 64x64 cell to count as changed
const BIG_DIFF_FRAC = 0.2; // fraction of changed cells → scroll/page change → clear highlight
const MAX_FRAME_W = 1600; // physical px sent to vision (native resolution up to this)
const JPEG_Q = 80;
const TEST_BOX = process.argv.includes("--test-box") || process.env.SURAKSHA_TEST_BOX === "1";

let overlay: BrowserWindow | null = null;
let pill: BrowserWindow | null = null;
let ws: WebSocket | null = null;
let wsOpen = false; // source of truth for the pill's "connected" line (it may load after the socket opens)
let captureMode: "on" | "off" | "paused" = "off";
let captureTimer: NodeJS.Timeout | null = null;
let lastGray: Uint8Array | null = null;
let forceFrame = false;
let capturing = false;
let recordable = false; // "Show in recordings": overlay + pill visible to screenshots/recording/screen share
let highlightRect: Rect | null = null; // display-relative DIP rect currently drawn
let lastMask: Uint8Array | null = null;
// Sensitive fields (Aadhaar/OTP/password/bank) the user has filled: painted over in every frame before
// it leaves the laptop. Display-relative DIP. Dropped on scroll/page change (the field moved).
let redactRects: Rect[] = [];

const renderer = (f: string) => path.join(__dirname, "..", "renderer", f);
const preload = path.join(__dirname, "preload.js");

function createOverlay() {
  const d = screen.getPrimaryDisplay();
  overlay = new BrowserWindow({
    x: d.bounds.x, y: d.bounds.y, width: d.bounds.width, height: d.bounds.height,
    transparent: true, frame: false, resizable: false, movable: false,
    alwaysOnTop: true, skipTaskbar: true, focusable: false, hasShadow: false,
    enableLargerThanScreen: true, show: false,
    webPreferences: { preload },
  });
  overlay.setIgnoreMouseEvents(true);
  overlay.setContentProtection(true);
  overlay.setAlwaysOnTop(true, "screen-saver");
  overlay.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  overlay.setBounds(d.bounds); // macOS may push it below the menu bar on create; renderer compensates
  overlay.loadFile(renderer("overlay.html"));
  overlay.once("ready-to-show", () => {
    overlay?.showInactive();
    overlay?.setBounds(screen.getPrimaryDisplay().bounds);
    measureOverlay();
    setTimeout(measureOverlay, 400); // macOS may move the window after show
    if (TEST_BOX) toOverlay("highlight", { rect: { x: 0.3 * d.bounds.width, y: 0.3 * d.bounds.height, width: 0.4 * d.bounds.width, height: 0.06 * d.bounds.height }, label: "Test box" });
  });
  overlay.on("move", measureOverlay);
  overlay.on("resize", measureOverlay);
}

// Where the overlay window REALLY is, relative to the primary display (DIP).
// macOS can place it below the menu bar or at y<0 while getBounds() still reports the requested
// bounds, so prefer the window server's answer (get-windows) and fall back to getBounds().
let overlayOrigin = { x: 0, y: 0 };
async function measureOverlay() {
  if (!overlay) return;
  const d = screen.getPrimaryDisplay();
  const eb = overlay.getBounds();
  let x = eb.x, y = eb.y, src = "getBounds";
  const mine = (await openWindows()).filter((w) => w.owner.processId === process.pid && w.bounds.width >= d.bounds.width - 2);
  if (mine.length) { x = mine[0].bounds.x; y = mine[0].bounds.y; src = "window-server"; }
  overlayOrigin = { x: x - d.bounds.x, y: y - d.bounds.y };
  console.log(`[overlay] origin ${src}=(${x},${y}) getBounds=(${eb.x},${eb.y},${eb.width}x${eb.height}) display=(${d.bounds.x},${d.bounds.y},${d.bounds.width}x${d.bounds.height}) scale=${d.scaleFactor}`);
  toOverlay("geometry", { offsetX: overlayOrigin.x, offsetY: overlayOrigin.y });
}

function createPill() {
  const d = screen.getPrimaryDisplay();
  const w = 380, h = 276;
  pill = new BrowserWindow({
    x: d.workArea.x + d.workArea.width - w - 24, y: d.workArea.y + 24, width: w, height: h,
    frame: false, resizable: false, alwaysOnTop: true, skipTaskbar: false, transparent: true, hasShadow: true,
    fullscreenable: false,
    webPreferences: { preload },
  });
  pill.setAlwaysOnTop(true, "screen-saver", 1); // above the overlay
  pill.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  pill.setContentProtection(true); // keep the pill out of screenshots sent to vision
  pill.loadFile(renderer("pill.html"));
  pill.webContents.on("did-finish-load", () => toPill("conn", { ok: wsOpen }));
  pill.once("ready-to-show", () => {
    checkPermissions();
    // debug flags: start visible to recordings / show calibration boxes
    if (process.argv.includes("--recordable")) setRecordable(true);
    if (process.argv.includes("--calibrate")) setTimeout(calibrate, 800);
  });
}

const toOverlay = (ch: string, data: unknown = {}) => overlay?.webContents.send(ch, data);
const toPill = (ch: string, data: unknown = {}) => pill?.webContents.send(ch, data);

// ---------- permissions ----------
async function checkPermissions() {
  if (process.platform !== "darwin") return;
  const scr = systemPreferences.getMediaAccessStatus("screen");
  let mic = systemPreferences.getMediaAccessStatus("microphone");
  if (mic === "not-determined") mic = (await systemPreferences.askForMediaAccess("microphone")) ? "granted" : "denied";
  if (scr !== "granted") {
    // Triggers the macOS prompt the first time; capture stays blank until the app restarts.
    desktopCapturer.getSources({ types: ["screen"], thumbnailSize: { width: 1, height: 1 } }).catch(() => {});
  }
  toPill("permissions", { screen: scr, mic });
}

// ---------- WebSocket ----------
function connect() {
  ws = new WebSocket(WS_URL);
  ws.on("open", () => { wsOpen = true; toPill("conn", { ok: true }); });
  ws.on("close", () => { wsOpen = false; toPill("conn", { ok: false }); ws = null; setTimeout(connect, 1500); });
  ws.on("error", () => {});
  ws.on("message", (raw) => {
    let msg: any;
    try { msg = JSON.parse(raw.toString()); } catch { return; }
    switch (msg.type) {
      case "highlight": showHighlight(msg); break;
      case "clear": clearOverlay(); break;
      case "redact": { const fr = frameRects.get(msg.frame_id) ?? lastCrop; if (fr) redactRects.push(bboxToRect(msg.bbox, fr)); break; }
      case "capture": setCapture(msg.mode); break;
      case "say": case "state": case "link": case "transcript": case "lang": case "sources": case "choice": case "choice.clear": toPill(msg.type, msg); break;
    }
  });
}

function wsSend(obj: unknown) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function clearOverlay() {
  highlightRect = null;
  toOverlay("clear");
}

// ---------- coordinates ----------
// bbox = [ymin, xmin, ymax, xmax] 0–1000 relative to the frame; frameRect = what the frame covered,
// in display-relative DIP. Result: display-relative DIP rect (overlay subtracts its own origin).
function bboxToRect(bbox: number[], frameRect: Rect): Rect {
  const [ymin, xmin, ymax, xmax] = bbox;
  return {
    x: frameRect.x + (xmin / 1000) * frameRect.width,
    y: frameRect.y + (ymin / 1000) * frameRect.height,
    width: ((xmax - xmin) / 1000) * frameRect.width,
    height: ((ymax - ymin) / 1000) * frameRect.height,
  };
}

// frame_id → the display-relative DIP rect that frame covered (bbox is relative to it)
const frameRects = new Map<number, Rect>();
let frameSeq = 0;

function showHighlight(msg: any) {
  const fr = frameRects.get(msg.frame_id) ?? lastCrop;
  if (!fr) return;
  highlightRect = bboxToRect(msg.bbox, fr);
  toOverlay("highlight", { rect: highlightRect, label: msg.label });
}

// ---------- browser window tracking ----------
// Last active window owned by a browser (ignores our own pill/overlay so clicking them keeps the target).
let browser: { name: string; bounds: Rect } | null = null;
let tracking = false;
async function trackBrowser() {
  if (tracking) return;
  tracking = true;
  const w = await activeWindow().finally(() => { tracking = false; });
  if (!w || w.owner.processId === process.pid) return;
  if (isBrowser(w)) browser = { name: w.owner.name, bounds: w.bounds };
}

// Browser window bounds clipped to the primary display, display-relative DIP. Full display if unknown.
function captureRect(): Rect {
  const d = screen.getPrimaryDisplay().bounds;
  if (!browser) return { x: 0, y: 0, width: d.width, height: d.height };
  const b = browser.bounds;
  const x0 = Math.max(b.x - d.x, 0), y0 = Math.max(b.y - d.y, 0);
  const x1 = Math.min(b.x - d.x + b.width, d.width), y1 = Math.min(b.y - d.y + b.height, d.height);
  if (x1 - x0 < 100 || y1 - y0 < 100) return { x: 0, y: 0, width: d.width, height: d.height };
  return { x: x0, y: y0, width: x1 - x0, height: y1 - y0 };
}

const sameRect = (a: Rect | null, b: Rect | null) =>
  !!a && !!b && Math.abs(a.x - b.x) < 1 && Math.abs(a.y - b.y) < 1 && Math.abs(a.width - b.width) < 1 && Math.abs(a.height - b.height) < 1;
let lastCrop: Rect | null = null;

// ---------- capture ----------
function setCapture(mode: "on" | "off" | "paused") {
  captureMode = mode;
  toPill("capture", { mode });
  if (mode === "on") {
    forceFrame = true;
    if (!captureTimer) captureTimer = setInterval(tick, CAPTURE_MS);
    tick();
  } else if (mode === "off") {
    if (captureTimer) clearInterval(captureTimer);
    captureTimer = null;
    lastGray = null;
    lastCrop = null;
    frameRects.clear();
    redactRects = [];
    clearOverlay();
  }
}

function gray64(img: Electron.NativeImage): Uint8Array {
  const bmp = img.resize({ width: 64, height: 64, quality: "good" }).toBitmap(); // BGRA
  const out = new Uint8Array(64 * 64);
  for (let i = 0; i < out.length; i++) out[i] = (bmp[i * 4] * 0.114 + bmp[i * 4 + 1] * 0.587 + bmp[i * 4 + 2] * 0.299) | 0;
  return out;
}

async function grabScreen(): Promise<Electron.NativeImage | null> {
  const d = screen.getPrimaryDisplay();
  const sources = await desktopCapturer.getSources({
    types: ["screen"],
    thumbnailSize: { width: Math.round(d.size.width * d.scaleFactor), height: Math.round(d.size.height * d.scaleFactor) },
  });
  const src = sources.find((s) => s.display_id === String(d.id)) || sources[0];
  if (!src || src.thumbnail.isEmpty()) return null;
  return src.thumbnail;
}

// 64×64 grid cells (over `crop`) covered by our own UI when it is visible to capture:
// the highlight (+10 px, plus room for its caption above/below) and the pill.
function ownUiMask(crop: Rect): Uint8Array {
  const m = new Uint8Array(64 * 64);
  if (!recordable) return m;
  const d = screen.getPrimaryDisplay().bounds;
  const rs: Rect[] = [];
  if (highlightRect) { const h = highlightRect; rs.push({ x: h.x - 10, y: h.y - 10 - 48, width: h.width + 20, height: h.height + 20 + 96 }); }
  if (pill) { const p = pill.getBounds(); rs.push({ x: p.x - d.x - 10, y: p.y - d.y - 10, width: p.width + 20, height: p.height + 20 }); }
  const cw = crop.width / 64, ch = crop.height / 64;
  for (const r of rs) {
    const c0 = Math.max(0, Math.floor((r.x - crop.x) / cw)), c1 = Math.min(63, Math.floor((r.x + r.width - crop.x) / cw));
    const r0 = Math.max(0, Math.floor((r.y - crop.y) / ch)), r1 = Math.min(63, Math.floor((r.y + r.height - crop.y) / ch));
    for (let y = r0; y <= r1; y++) for (let x = c0; x <= c1; x++) m[y * 64 + x] = 1;
  }
  return m;
}

// Paint rects (display DIP, +8 px) solid grey on an image that covers `crop`.
function redact(img: Electron.NativeImage, crop: Rect, rects: Rect[]): Electron.NativeImage {
  const { width: W, height: H } = img.getSize();
  const bmp = Buffer.from(img.toBitmap()); // BGRA
  const kx = W / crop.width, ky = H / crop.height;
  for (const r of rects) {
    const x0 = Math.max(0, Math.floor((r.x - 8 - crop.x) * kx)), x1 = Math.min(W, Math.ceil((r.x + r.width + 8 - crop.x) * kx));
    const y0 = Math.max(0, Math.floor((r.y - 8 - crop.y) * ky)), y1 = Math.min(H, Math.ceil((r.y + r.height + 8 - crop.y) * ky));
    for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) { const i = (y * W + x) * 4; bmp[i] = bmp[i + 1] = bmp[i + 2] = 0x80; bmp[i + 3] = 0xff; }
  }
  return nativeImage.createFromBitmap(bmp, { width: W, height: H });
}

async function tick() {
  if (captureMode !== "on" || capturing) return;
  capturing = true;
  try {
    const shot = await grabScreen();
    if (!shot) return;
    const crop = captureRect();
    if (!sameRect(crop, lastCrop)) { // browser moved/resized/switched: old boxes and diff baseline are stale
      if (lastCrop) clearOverlay();
      redactRects = [];
      lastGray = null;
      forceFrame = true;
    }
    const k = shot.getSize().width / screen.getPrimaryDisplay().bounds.width; // physical px per DIP
    const img = shot.crop({ x: Math.round(crop.x * k), y: Math.round(crop.y * k), width: Math.round(crop.width * k), height: Math.round(crop.height * k) });
    const g = gray64(img);
    const mask = ownUiMask(crop);
    let changedCells = 0, counted = 0;
    if (lastGray) for (let i = 0; i < g.length; i++) {
      if (mask[i] || lastMask?.[i]) continue; // our own pixels (only visible when recordable) aren't page changes
      counted++;
      if (Math.abs(g[i] - lastGray[i]) > CELL_DIFF) changedCells++;
    }
    lastMask = mask;
    const big = !!lastGray && counted > 0 && changedCells / counted > BIG_DIFF_FRAC;
    if (big) { clearOverlay(); redactRects = []; } // scroll / new page
    if (lastGray && changedCells === 0 && !forceFrame) return;
    forceFrame = false;
    lastGray = g;
    lastCrop = crop;
    let out = img.getSize().width > MAX_FRAME_W ? img.resize({ width: MAX_FRAME_W, quality: "best" }) : img;
    if (redactRects.length) out = redact(out, crop, redactRects);
    const s = out.getSize();
    const frame_id = ++frameSeq;
    frameRects.set(frame_id, crop);
    if (frameRects.size > 20) frameRects.delete(frameRects.keys().next().value!);
    wsSend({ type: "frame", frame_id, jpeg_b64: out.toJPEG(JPEG_Q).toString("base64"), w: s.width, h: s.height, big_change: big, browser: browser?.name ?? null, own_ui_visible: recordable });
  } catch {
    // capture failures are expected before screen permission is granted
  } finally {
    capturing = false;
  }
}

// ---------- IPC from pill ----------
ipcMain.on("start", (_e, { lang }) => { wsSend({ type: "start", lang }); });
ipcMain.on("stop", () => { wsSend({ type: "stop" }); setCapture("off"); });
ipcMain.on("next", () => { wsSend({ type: "next" }); forceFrame = true; if (captureMode === "on") tick(); });
ipcMain.on("ask", (_e, { audio_b64, lang }) => wsSend({ type: "ask", audio_b64, lang }));
ipcMain.on("lang", (_e, { lang }) => wsSend({ type: "lang", lang }));
ipcMain.on("choice", (_e, { id, value }) => wsSend({ type: "choice", id, value }));
ipcMain.on("open-link", (_e, { url }) => { if (/^https:\/\//.test(url)) shell.openExternal(url); });
ipcMain.on("quit", () => app.quit());
ipcMain.on("hello", () => toPill("conn", { ok: wsOpen }));

ipcMain.on("recordable", (_e, { on }) => setRecordable(!!on));
function setRecordable(on: boolean) {
  recordable = on;
  overlay?.setContentProtection(!recordable);
  pill?.setContentProtection(!recordable);
  toOverlay("recordable", { on: recordable });
  lastGray = null; // baseline changes once our UI becomes visible in captures
  forceFrame = true;
  toPill("recordable", { on: recordable });
}

// Debug: magenta boxes at the 4 display corners + centre (+ the browser capture rect) for 10 s.
// If the drawn boxes don't sit exactly in the screen corners, the overlay origin is off.
let calibTimer: NodeJS.Timeout | null = null;
ipcMain.on("calibrate", () => calibrate());
async function calibrate() {
  await measureOverlay();
  await trackBrowser();
  const { width: W, height: H } = screen.getPrimaryDisplay().bounds;
  const S = 90;
  const rects: Rect[] = [
    { x: 0, y: 0, width: S, height: S }, { x: W - S, y: 0, width: S, height: S },
    { x: 0, y: H - S, width: S, height: S }, { x: W - S, y: H - S, width: S, height: S },
    { x: W / 2 - 60, y: H / 2 - 30, width: 120, height: 60 },
  ];
  if (browser) rects.push(captureRect());
  toOverlay("calib", { rects });
  toPill("status", { text: `overlay origin ${overlayOrigin.x},${overlayOrigin.y} · browser ${browser ? browser.name : "none"}` });
  if (calibTimer) clearTimeout(calibTimer);
  calibTimer = setTimeout(() => toOverlay("calib", { rects: [] }), 10000);
}

app.whenReady().then(() => {
  session.defaultSession.setPermissionRequestHandler((_wc, perm, cb) => cb(perm === "media"));
  createOverlay();
  createPill();
  connect();
  setInterval(trackBrowser, 500);
  screen.on("display-metrics-changed", () => { overlay?.setBounds(screen.getPrimaryDisplay().bounds); measureOverlay(); });
});

app.on("window-all-closed", () => app.quit());
