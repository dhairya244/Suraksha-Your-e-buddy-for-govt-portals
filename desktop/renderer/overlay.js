// bbox = [ymin, xmin, ymax, xmax], 0–1000, relative to the full primary display.
const box = document.getElementById("box");
const cap = document.getElementById("cap");
let geo = { offsetX: 0, offsetY: 0, width: window.innerWidth, height: window.innerHeight };
const PAD = 6;

window.suraksha.on("geometry", (g) => { geo = g; });

window.suraksha.on("highlight", ({ bbox, label }) => {
  const [ymin, xmin, ymax, xmax] = bbox;
  const left = (xmin / 1000) * geo.width - geo.offsetX - PAD;
  const top = (ymin / 1000) * geo.height - geo.offsetY - PAD;
  const w = ((xmax - xmin) / 1000) * geo.width + PAD * 2;
  const h = ((ymax - ymin) / 1000) * geo.height + PAD * 2;
  Object.assign(box.style, { left: left + "px", top: top + "px", width: w + "px", height: h + "px", display: "block" });
  if (label) {
    cap.textContent = label;
    cap.style.display = "block";
    const capTop = top - 40 > 0 ? top - 40 : top + h + 8;
    Object.assign(cap.style, { left: Math.max(4, left) + "px", top: capTop + "px" });
  } else cap.style.display = "none";
});

window.suraksha.on("clear", () => { box.style.display = "none"; cap.style.display = "none"; });
