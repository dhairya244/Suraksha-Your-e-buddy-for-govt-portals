"""Shared helpers for the data pipeline: discover → fetch → extract → check → merge.

Config lives in scripts/sources.yaml (per source) and .env (keys, engines, budget, delays).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
import time
import unicodedata
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urldefrag, urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.config import env, env_float, env_int, redact  # noqa: E402

SOURCES_YAML = ROOT / "scripts" / "sources.yaml"
BUDGET_JSON = ROOT / "scripts" / "budget.json"
RAW = ROOT / "data" / "raw"
EXTRACTED = ROOT / "data" / "extracted"
DATA = ROOT / "data"

BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
LOGIN_RE = re.compile(r"(log-?in|sign-?in|signup|\bsso\b|otp|password|citizenlogin|useractivation)", re.I)


# ---------------------------------------------------------------- small utils
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def slug(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:max_len]


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def domain_allowed(url: str, suffixes: list[str]) -> bool:
    h = host(url)
    return any(h == s or h.endswith("." + s) for s in suffixes)


def canonical_url(url: str) -> str:
    """https, lower-case host, no trailing slash (except root), no fragment."""
    u = urlparse(urldefrag(url)[0])
    path = u.path.rstrip("/") if u.path not in ("", "/") else "/"
    return u._replace(scheme="https", netloc=u.netloc.lower(), path=path).geturl()


def is_pdf(url: str) -> bool:
    return urldefrag(url)[0].lower().split("?")[0].endswith(".pdf")


def looks_like_login(url: str, title: str = "") -> bool:
    return bool(LOGIN_RE.search(url) or LOGIN_RE.search(title or ""))


# ---------------------------------------------------------------- config
@dataclass
class SourceCfg:
    id: str
    raw: dict

    def __getitem__(self, k):
        return self.raw[k]

    def get(self, k, default=None):
        return self.raw.get(k, default)

    @property
    def engine(self) -> str:
        return self.raw.get("engine") or env("FETCH_ENGINE_DEFAULT", "auto")

    @property
    def dir(self) -> Path:
        return RAW / self.id

    @property
    def urls_path(self) -> Path:
        return self.dir / "urls.jsonl"

    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.jsonl"


def load_config() -> dict:
    return yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))


def load_sources() -> dict[str, SourceCfg]:
    cfg = load_config()
    return {sid: SourceCfg(sid, s) for sid, s in cfg["sources"].items()}


def get_source(sid: str) -> SourceCfg:
    sources = load_sources()
    if sid not in sources:
        sys.exit(f"unknown source {sid!r}; known: {', '.join(sources)}")
    return sources[sid]


def official_domains() -> list[str]:
    cfg = load_config()
    return cfg.get("official_domain_suffixes", ["gov.in", "nic.in"])


# ---------------------------------------------------------------- URL filtering / priority
def allow_match(src: SourceCfg, url: str, text: str = "") -> bool:
    return any(re.search(p, f"{url} {text}", re.I) for p in src.get("allow") or [])


def link_matches(cfg: dict, src: SourceCfg, url: str, text: str) -> bool:
    hay = f"{url} {text}"
    include = src.get("include") or cfg["defaults"]["include"]
    exclude = (cfg["defaults"]["exclude"] or []) + (src.get("exclude") or [])
    if any(re.search(p, hay, re.I) for p in src.get("allow") or []):
        return True
    if any(re.search(p, hay, re.I) for p in exclude):
        return False
    return any(re.search(p, hay, re.I) for p in include)


def match_score(src: SourceCfg, url: str, text: str) -> int:
    """How many priority patterns (any tier) match — ranks URLs within a tier."""
    hay = f"{url} {text}"
    return sum(bool(re.search(p, hay, re.I)) for ps in (src.get("priority") or {}).values() for p in ps)


def priority_for(src: SourceCfg, url: str, text: str) -> int:
    """1 = fetch first. First matching tier in sources.yaml `priority` wins; else 3."""
    hay = f"{url} {text}"
    for tier, patterns in sorted((src.get("priority") or {}).items()):
        if any(re.search(p, hay, re.I) for p in patterns):
            return int(tier)
    return 3


# ---------------------------------------------------------------- Firecrawl budget
class Budget:
    """Persisted Firecrawl credit ledger (scripts/budget.json)."""

    def __init__(self) -> None:
        self.limit = env_int("FIRECRAWL_CREDIT_BUDGET", 1000)
        self.data = json.loads(BUDGET_JSON.read_text()) if BUDGET_JSON.exists() else {"credits_used": 0, "log": []}

    @property
    def used(self) -> int:
        return int(self.data["credits_used"])

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def can_spend(self, n: int) -> bool:
        return self.used + n <= self.limit

    def spend(self, n: int, kind: str, source: str, url: str, estimated: bool = False) -> None:
        self.data["credits_used"] = self.used + int(n)
        self.data["log"].append({"ts": now_iso(), "kind": kind, "source": source, "url": url,
                                 "credits": int(n), "estimated": estimated})
        BUDGET_JSON.write_text(json.dumps(self.data, indent=2) + "\n")


def firecrawl_client():
    from firecrawl import Firecrawl

    key = env("FIRECRAWL_API_KEY")
    if not key:
        sys.exit("FIRECRAWL_API_KEY is not set in .env")
    return Firecrawl(api_key=key)


# ---------------------------------------------------------------- HTML / PDF → markdown
BOILERPLATE_TAGS = ["script", "style", "noscript", "svg", "iframe", "nav", "footer", "header", "img"]
BOILERPLATE_ATTR = re.compile(r"(footer|calendar|holiday|navbar|menu|breadcrumb|skip|modal|cookie|social|visitor)", re.I)
MAIN_CANDIDATES = ["main", "[role=main]", "#MainContent", "#maincontent", "#content", ".main-content"]


def html_to_markdown(html: str) -> str:
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(BOILERPLATE_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.attrs is None:
            continue
        attrs = " ".join([tag.get("id") or ""] + (tag.get("class") or []))
        if attrs.strip() and BOILERPLATE_ATTR.search(attrs) and tag.name not in ("body", "html"):
            tag.decompose()
    node = None
    for sel in MAIN_CANDIDATES:
        cand = soup.select_one(sel)
        if cand and len(cand.get_text(strip=True)) > 200:
            node = cand
            break
    node = node or soup.body or soup
    md = markdownify(str(node), heading_style="ATX")
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def pdf_to_markdown(data: bytes) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts = []
    for i, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        parts.append(f"## Page {i}\n\n{text}")
    return "\n\n".join(parts).strip(), len(reader.pages)


# ---------------------------------------------------------------- local browser (Playwright)
@dataclass
class PageResult:
    url: str
    final_url: str
    status: int | None
    title: str
    html: str


class LocalBrowser:
    """One polite Chromium session: 1 request at a time, delay between requests, robots.txt."""

    def __init__(self, delay_s: float | None = None) -> None:
        self.delay_s = env_float("LOCAL_FETCH_DELAY_SECONDS", 2.5) if delay_s is None else delay_s
        self._last = 0.0
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def __enter__(self) -> "LocalBrowser":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self.ctx = self._browser.new_context(user_agent=BROWSER_UA, locale="en-IN", accept_downloads=True)
        self.page = self.ctx.new_page()
        return self

    def __exit__(self, *exc) -> None:
        self._browser.close()
        self._pw.stop()

    def _polite(self) -> None:
        wait = self._last + self.delay_s - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            self._polite()
            rp = None
            try:
                r = self.ctx.request.get(base + "/robots.txt", timeout=20000, max_redirects=0)
                if r.ok and "text/plain" in (r.headers.get("content-type") or ""):
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(r.text().splitlines())
            except Exception:  # noqa: BLE001 - no robots.txt → allowed
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return rp is None or rp.can_fetch(BROWSER_UA, url)

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:  # noqa: BLE001 - busy pages never go idle; use what loaded
            pass

    def run_actions(self, actions: list[dict]) -> None:
        """Actions: {goto: url} · {select: css, value: v} · {eval: js} · {click: css}; add wait_navigation: true."""
        for a in actions:
            self._polite()
            nav = a.get("wait_navigation", False)

            def do(a=a):
                if "goto" in a:
                    self.page.goto(a["goto"], wait_until="domcontentloaded", timeout=60000)
                elif "select" in a:
                    self.page.select_option(a["select"], a["value"])
                elif "eval" in a:
                    self.page.evaluate(a["eval"])
                elif "click" in a:
                    self.page.click(a["click"])

            if nav:
                with self.page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
                    do()
            else:
                do()
            self._settle()

    def open(self, url: str, actions: list[dict] | None = None) -> PageResult:
        status = None
        if actions:
            self.run_actions(actions)
        else:
            self._polite()
            resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            status = resp.status if resp else None
            self._settle()
        return PageResult(url=url, final_url=self.page.url, status=status,
                          title=self.page.title(), html=self.page.content())

    def links(self, postback_selector: str | None = None) -> list[tuple[str, str, str | None]]:
        """(href, text, postback_js) for every link on the current page."""
        rows = self.page.eval_on_selector_all("a[href]", """els => els.map(e => {
            let t = (e.innerText || e.title || '').trim().replace(/\\s+/g, ' ');
            const raw = e.getAttribute('href') || '';
            if (/read more/i.test(t) || (raw.startsWith('javascript:') && t.length < 12)) {
                let c = e.parentElement;
                for (let i = 0; i < 4 && c && (c.innerText || '').trim().length < 20; i++) c = c.parentElement;
                if (c) t = (c.innerText || '').trim().replace(/\\s+/g, ' ');
            }
            return [e.href, e.getAttribute('href') || '', t.slice(0, 160)];
        })""")
        out = []
        for href, raw_href, text in rows:
            if raw_href.startswith("javascript:"):
                if postback_selector and "__doPostBack" in raw_href and re.search(postback_selector, raw_href):
                    out.append((href, text, raw_href[len("javascript:"):]))
                continue
            if href.startswith("http"):
                out.append((urldefrag(href)[0], text, None))
        return out

    def download(self, url: str) -> tuple[bytes, str, int]:
        """Download via fetch() inside the page, so Chromium's TLS stack is used (some gov sites
        serve an incomplete certificate chain that only a full browser completes)."""
        import base64

        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if not self.page.url.startswith(origin):
            self._polite()
            self.page.goto(origin + "/", wait_until="domcontentloaded", timeout=60000)
        self._polite()
        status, ctype, b64 = self.page.evaluate("""async (u) => {
            const r = await fetch(u, {credentials: 'include'});
            const blob = await r.blob();
            const b64 = await new Promise(res => { const fr = new FileReader();
                fr.onload = () => res(fr.result.split(',')[1] || ''); fr.readAsDataURL(blob); });
            return [r.status, r.headers.get('content-type') || '', b64];
        }""", url)
        return base64.b64decode(b64), ctype, status


# ---------------------------------------------------------------- text normalisation for quote checks
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_MARKS = re.compile(r"[*_`#>|]+")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', " ": " ", "–": "-", "—": "-"})


def normalise_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).translate(_QUOTES)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_MARKS.sub(" ", text)
    text = text.replace("\\", "")
    return re.sub(r"\s+", " ", text).strip()


__all__ = [n for n in dir() if not n.startswith("_")] + ["redact", "env", "env_int", "env_float"]
