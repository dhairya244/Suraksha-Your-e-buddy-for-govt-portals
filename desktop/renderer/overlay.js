// Draws boxes given display-relative DIP rects; subtracts the overlay window's real origin.
const box = document.getElementById("box");
const cap = document.getElementById("cap");
const layer = document.getElementById("calib");
let geo = { offsetX: 0, offsetY: 0 };
const PAD = 4;

window.suraksha.on("geometry", (g) => { geo = g; });

function place(el, r, pad) {
  const left = r.x - geo.offsetX - pad, top = r.y - geo.offsetY - pad;
  Object.assign(el.style, { left: left + "px", top: top + "px", width: r.width + pad * 2 + "px", height: r.height + pad * 2 + "px", display: "block" });
  return { left, top, w: r.width + pad * 2, h: r.height + pad * 2 };
}

window.suraksha.on("highlight", ({ rect, label }) => {
  const p = place(box, rect, PAD);
  if (label) {
    cap.textContent = label;
    cap.style.display = "block";
    const capTop = p.top - 40 > 0 ? p.top - 40 : p.top + p.h + 8;
    Object.assign(cap.style, { left: Math.max(4, p.left) + "px", top: capTop + "px" });
  } else cap.style.display = "none";
});

// Calibration: boxes at known display rects, each labelled with its coordinates.
window.suraksha.on("calib", ({ rects }) => {
  layer.innerHTML = "";
  for (const r of rects) {
    const el = document.createElement("div");
    el.className = "cal";
    place(el, r, 0);
    el.textContent = `${Math.round(r.x)},${Math.round(r.y)} ${Math.round(r.width)}×${Math.round(r.height)}`;
    layer.appendChild(el);
  }
});

window.suraksha.on("recordable", ({ on }) => document.body.classList.toggle("recordable", !!on));

window.suraksha.on("clear", () => { box.style.display = "none"; cap.style.display = "none"; layer.innerHTML = ""; });
