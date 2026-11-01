// Suraksha desktop main process: overlay + pill windows, screen capture, WebSocket to backend.
// Never logs screenshots, transcripts or audio.
import { app, BrowserWindow, desktopCapturer, ipcMain, nativeImage, screen, session, shell, systemPreferences } from "electron";
import path from "path";
import WebSocket from "ws";

const PORT = process.env.BACKEND_PORT || "8765";
const WS_URL = `ws://127.0.0.1:${PORT}/ws/session`;
const CAPTURE_MS = 1500;
const CELL_DIFF = 10; // grayscale delta (0-255) for a 64x64 cell to count as changed
const BIG_DIFF_FRAC = 0.2; // fraction of changed cells → scroll/page change → clear highlight
const TEST_BOX = process.argv.includes("--test-box") || process.env.SURAKSHA_TEST_BOX === "1";

let overlay: BrowserWindow | null = null;
let pill: BrowserWindow | null = null;
let ws: WebSocket | null = null;
let captureMode: "on" | "off" | "paused" = "off";
let captureTimer: NodeJS.Timeout | null = null;
let lastGray: Uint8Array | null = null;
let forceFrame = false;
let capturing = false;

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
    sendOverlayGeometry();
    if (TEST_BOX) toOverlay("highlight", { bbox: [300, 300, 360, 700], label: "Test box (M1)" });
  });
}

function sendOverlayGeometry() {
  if (!overlay) return;
  const d = screen.getPrimaryDisplay();
  const b = overlay.getBounds();
  // Renderer draws in window coords; bbox is relative to the full display.
  toOverlay("geometry", { offsetX: b.x - d.bounds.x, offsetY: b.y - d.bounds.y, width: d.bounds.width, height: d.bounds.height });
}

function createPill() {
  const d = screen.getPrimaryDisplay();
  const w = 380, h = 200;
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
  pill.once("ready-to-show", () => checkPermissions());
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
  ws.on("open", () => toPill("conn", { ok: true }));
  ws.on("close", () => { toPill("conn", { ok: false }); ws = null; setTimeout(connect, 1500); });
  ws.on("error", () => {});
  ws.on("message", (raw) => {
    let msg: any;
    try { msg = JSON.parse(raw.toString()); } catch { return; }
    switch (msg.type) {
      case "highlight": toOverlay("highlight", msg); break;
      case "clear": toOverlay("clear"); break;
      case "capture": setCapture(msg.mode); break;
      case "say": case "state": case "link": case "transcript": toPill(msg.type, msg); break;
    }
  });
}

function wsSend(obj: unknown) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

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
    toOverlay("clear");
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

async function tick() {
  if (captureMode !== "on" || capturing) return;
  capturing = true;
  try {
    const img = await grabScreen();
    if (!img) return;
    const g = gray64(img);
    let changedCells = 0;
    if (lastGray) for (let i = 0; i < g.length; i++) if (Math.abs(g[i] - lastGray[i]) > CELL_DIFF) changedCells++;
    const changed = !lastGray || changedCells > 0;
    if (lastGray && changedCells / g.length > BIG_DIFF_FRAC) toOverlay("clear"); // scroll / new page
    if (!changed && !forceFrame) return;
    forceFrame = false;
    lastGray = g;
    const size = img.getSize();
    const small = size.width > 1280 ? img.resize({ width: 1280, quality: "good" }) : img;
    const s = small.getSize();
    wsSend({ type: "frame", jpeg_b64: small.toJPEG(70).toString("base64"), w: s.width, h: s.height, big_change: changedCells / g.length > BIG_DIFF_FRAC });
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
ipcMain.on("open-link", (_e, { url }) => { if (/^https:\/\//.test(url)) shell.openExternal(url); });
ipcMain.on("quit", () => app.quit());

app.whenReady().then(() => {
  session.defaultSession.setPermissionRequestHandler((_wc, perm, cb) => cb(perm === "media"));
  createOverlay();
  createPill();
  connect();
  screen.on("display-metrics-changed", () => { overlay?.setBounds(screen.getPrimaryDisplay().bounds); sendOverlayGeometry(); });
});

app.on("window-all-closed", () => app.quit());
// silence unused import warning in strict builds
void nativeImage;
