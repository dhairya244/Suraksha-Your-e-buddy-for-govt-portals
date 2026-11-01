// Window geometry from the OS window server (get-windows). On macOS these are CoreGraphics
// global points (top-left origin of the primary display) = Electron DIP screen coords.
// get-windows is ESM-only, so load it with a real dynamic import (tsc would turn import() into require()).
export type Rect = { x: number; y: number; width: number; height: number };
export type Win = { title: string; bounds: Rect; owner: { name: string; processId: number; bundleId?: string; path?: string } };

const esmImport = new Function("m", "return import(m)") as (m: string) => Promise<any>;
let gw: any = null;
let gwFailed = false;

async function lib(): Promise<any | null> {
  if (gw || gwFailed) return gw;
  try { gw = await esmImport("get-windows"); } catch (e) { gwFailed = true; console.warn("[windows] get-windows unavailable:", (e as Error).message); }
  return gw;
}

// No titles/URLs needed — only bounds + owner. Skipping both permission checks keeps each call ~20 ms;
// with the screen-recording check the helper binary can stall ~30 s.
const opts = { accessibilityPermission: false, screenRecordingPermission: false };
const TIMEOUT_MS = 2000;
const withTimeout = <T>(p: Promise<T>, fallback: T): Promise<T> =>
  Promise.race([p.catch(() => fallback), new Promise<T>((r) => setTimeout(() => r(fallback), TIMEOUT_MS))]);

export async function openWindows(): Promise<Win[]> {
  const g = await lib();
  if (!g) return [];
  return (await withTimeout(g.openWindows(opts) as Promise<Win[]>, [])) ?? [];
}

export async function activeWindow(): Promise<Win | undefined> {
  const g = await lib();
  if (!g) return undefined;
  return withTimeout(g.activeWindow(opts) as Promise<Win | undefined>, undefined);
}

const BROWSER_RE = /chrome|chromium|edge|brave|firefox|safari|arc|opera|vivaldi/i;

export function isBrowser(w: Win): boolean {
  return BROWSER_RE.test(w.owner.name) || BROWSER_RE.test(w.owner.bundleId || "") || BROWSER_RE.test(w.owner.path || "");
}
