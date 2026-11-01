const S = window.suraksha;
const $ = (id) => document.getElementById(id);
let lang = "hi";
let linkUrl = null;
let audio = null;
let rec = null, recChunks = [], recStream = null;
let connected = false, permMsg = "";

function status(t) { $("status").textContent = t; }
function refreshStatus(extra) {
  status(permMsg || extra || (connected ? "connected" : "backend not connected…"));
}

// ---------- playback ----------
function stopAudio() { if (audio) { audio.pause(); audio.src = ""; audio = null; } }
function play(b64) {
  stopAudio();
  if (!b64) return;
  audio = new Audio("data:audio/wav;base64," + b64);
  audio.play().catch(() => {});
}

// ---------- buttons ----------
$("lang").onclick = () => {
  lang = lang === "hi" ? "en" : "hi";
  $("lang").textContent = lang === "hi" ? "हिंदी" : "EN";
  S.send("lang", { lang });
};
$("start").onclick = () => { stopAudio(); S.send("start", { lang }); };
$("next").onclick = () => { stopAudio(); S.send("next"); };
$("stop").onclick = () => { stopAudio(); S.send("stop"); $("caption").textContent = "Ruk gaya. Phir se shuru karne ke liye Start guiding dabaiye."; };
$("rec").onchange = (e) => S.send("recordable", { on: e.target.checked });
$("calib").onclick = () => S.send("calibrate");
$("x").onclick = () => S.send("quit");
$("link").onclick = () => { if (linkUrl) S.send("open-link", { url: linkUrl }); };

// ---------- hold to ask: MediaRecorder → decode → 16 kHz mono WAV ----------
const mic = $("mic");
async function recStart() {
  stopAudio(); // barge-in
  if (rec) return;
  try {
    recStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  } catch { status("Microphone permission nahi mili (System Settings → Privacy → Microphone)"); return; }
  recChunks = [];
  rec = new MediaRecorder(recStream);
  rec.ondataavailable = (e) => { if (e.data.size) recChunks.push(e.data); };
  rec.onstop = onRecStop;
  rec.start();
  mic.classList.add("rec"); mic.textContent = "🔴 Listening…";
  setTimeout(() => { if (rec && rec.state === "recording") recEnd(); }, 28000); // STT REST limit < 30 s
}
function recEnd() {
  if (!rec || rec.state !== "recording") return;
  rec.stop();
  mic.classList.remove("rec"); mic.textContent = "🎤 Hold to ask";
}
async function onRecStop() {
  recStream.getTracks().forEach((t) => t.stop());
  const blob = new Blob(recChunks, { type: rec.mimeType });
  rec = null;
  try {
    const buf = await blob.arrayBuffer();
    const ctx = new AudioContext();
    const decoded = await ctx.decodeAudioData(buf);
    ctx.close();
    if (decoded.duration < 0.4) { status("Bahut chhota — button dabakar rakhiye aur boliye"); return; }
    const off = new OfflineAudioContext(1, Math.ceil(decoded.duration * 16000), 16000);
    const src = off.createBufferSource(); src.buffer = decoded; src.connect(off.destination); src.start();
    const rendered = await off.startRendering();
    S.send("ask", { audio_b64: wavB64(rendered.getChannelData(0), 16000), lang });
    status("Soch raha hoon…");
  } catch { status("Audio record nahi ho paya"); }
}
function wavB64(f32, rate) {
  const n = f32.length, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) { const s = Math.max(-1, Math.min(1, f32[i])); v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true); }
  const bytes = new Uint8Array(buf); let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}
mic.addEventListener("pointerdown", (e) => { mic.setPointerCapture(e.pointerId); recStart(); });
mic.addEventListener("pointerup", recEnd);
mic.addEventListener("pointercancel", recEnd);

// ---------- messages from main ----------
S.on("conn", ({ ok }) => { connected = ok; refreshStatus(); });
S.on("permissions", ({ screen, mic: m }) => {
  if (screen !== "granted") {
    permMsg = "Screen Recording permission chahiye: System Settings → Privacy & Security → Screen Recording → allow, phir app restart karein.";
    $("caption").textContent = permMsg;
  } else if (m !== "granted") permMsg = "Microphone permission chahiye: System Settings → Privacy & Security → Microphone.";
  refreshStatus();
});
S.on("say", ({ text, audio_b64 }) => { $("caption").textContent = text; play(audio_b64); refreshStatus(); });
S.on("state", ({ value }) => refreshStatus(value));
S.on("link", ({ url, title }) => { linkUrl = url; const b = $("link"); b.style.display = "inline-block"; b.title = url; b.textContent = "Open: " + (title || "official link").slice(0, 28); });
S.on("capture", ({ mode }) => {
  const dot = $("dot"); dot.className = "dot" + (mode === "on" ? " on" : mode === "paused" ? " paused" : "");
  $("watching").textContent = mode === "on" ? "is watching" : mode === "paused" ? "paused (privacy)" : "";
});
S.on("status", ({ text }) => status(text));
S.on("recordable", ({ on }) => { $("rec").checked = !!on; });
S.on("transcript", ({ text }) => { $("caption").textContent = "🗣 " + text; status("Soch rahi hoon…"); });
S.on("lang", ({ value }) => { lang = value; $("lang").textContent = lang === "hi" ? "हिंदी" : lang === "gu" ? "ગુજરાતી" : "EN"; });
