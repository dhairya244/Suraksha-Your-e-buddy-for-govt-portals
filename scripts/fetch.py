"""Fetch discovered URLs → data/raw/<source>/<sha1(url)>.md + meta.jsonl.

  .venv/bin/python scripts/fetch.py --source digital_gujarat --limit 5 --preview 300
  .venv/bin/python scripts/fetch.py --source myscheme --engine firecrawl

Skips URLs already fetched. Firecrawl credits are tracked in scripts/budget.json and the run stops
before FIRECRAWL_CREDIT_BUDGET would be exceeded. PDFs are always downloaded locally (0 credits).
Local fetches: one request at a time, LOCAL_FETCH_DELAY_SECONDS apart, robots.txt respected,
login pages skipped.
"""
from __future__ import annotations

import argparse
import re
import sys

import httpx

from pipeline import (
    BROWSER_UA, Budget, LocalBrowser, SourceCfg, allow_match, append_jsonl, canonical_url,
    domain_allowed, firecrawl_client, get_source, html_to_markdown, is_pdf, link_matches,
    load_config, looks_like_login, now_iso, pdf_to_markdown, read_jsonl, redact, sha1,
)

DONE_STATUSES = {"ok", "skipped_login", "robots_disallowed"}
MIN_CHARS = 50
BLOCKED_RE = re.compile(r"^#+\s*403\b[\s\S]{0,40}Forbidden", re.M)  # 403 page served with HTTP 200


class Fetcher:
    def __init__(self, src: SourceCfg, engine: str) -> None:
        self.src, self.engine = src, engine
        self.budget = Budget()
        self.run_credits = 0
        self._browser: LocalBrowser | None = None
        self._fc = None
        self.firecrawl_exhausted = False

    # -- lazily started backends
    @property
    def browser(self) -> LocalBrowser:
        if self._browser is None:
            self._browser = LocalBrowser().__enter__()
            if self.src.get("setup"):
                self._browser.run_actions(self.src["setup"])
        return self._browser

    @property
    def fc(self):
        if self._fc is None:
            self._fc = firecrawl_client()
        return self._fc

    def close(self) -> None:
        if self._browser:
            self._browser.__exit__(None, None, None)

    # -- engines
    def via_firecrawl(self, url: str) -> dict:
        if not self.budget.can_spend(1):
            self.firecrawl_exhausted = True
            raise RuntimeError(f"Firecrawl budget reached ({self.budget.used}/{self.budget.limit})")
        doc = self.fc.scrape(url, formats=["markdown"], only_main_content=True,
                             wait_for=self.src.get("wait_for_ms"), timeout=90000)
        credits = (doc.metadata.credits_used if doc.metadata else None) or 1
        self.budget.spend(credits, "scrape", self.src.id, url)
        self.run_credits += credits
        md = (doc.markdown or "").strip()
        meta = doc.metadata
        return {"md": md, "engine": "firecrawl", "credits_used": credits,
                "final_url": getattr(meta, "url", None) or url, "title": getattr(meta, "title", "") or "",
                "http_status": getattr(meta, "status_code", None)}

    def via_local(self, entry: dict) -> dict:
        url = entry["url"]
        b = self.browser
        target = entry["actions"][0]["goto"] if entry.get("actions") else url
        if not b.allowed(target):
            return {"status": "robots_disallowed"}
        res = b.open(url, entry.get("actions"))
        if looks_like_login(res.final_url, res.title) and not allow_match(self.src, res.final_url):
            return {"status": "skipped_login", "final_url": res.final_url}
        self.collect_pdfs(entry)
        return {"md": html_to_markdown(res.html), "engine": "local", "credits_used": 0,
                "final_url": res.final_url, "title": res.title, "http_status": res.status}

    def via_pdf(self, url: str) -> dict:
        if self.src.engine == "local":
            data, ctype, status = self.browser.download(url)
        else:
            r = httpx.get(url, headers={"User-Agent": BROWSER_UA}, follow_redirects=True, timeout=90)
            data, ctype, status = r.content, r.headers.get("content-type", ""), r.status_code
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        if not data.startswith(b"%PDF"):
            raise RuntimeError(f"not a PDF (content-type {ctype})")
        md, pages = pdf_to_markdown(data)
        (self.src.dir / "pdf").mkdir(parents=True, exist_ok=True)
        (self.src.dir / "pdf" / f"{sha1(url)}.pdf").write_bytes(data)
        out = {"md": md, "engine": "local-pdf", "credits_used": 0, "final_url": url, "title": "",
               "http_status": status, "pdf_pages": pages}
        if len(md.replace("## Page", "").strip()) < 200:
            out["note"] = "little or no text layer (scanned PDF?)"
        return out

    def collect_pdfs(self, entry: dict) -> None:
        """Queue PDFs linked from a local page (they become new urls.jsonl entries)."""
        cfg = load_config()
        known = {r["url"] for r in read_jsonl(self.src.urls_path)}
        cap = self.src.get("max_urls", cfg["defaults"]["max_urls"])
        new = []
        for href, text, _ in self.browser.links():
            if not is_pdf(href) or not domain_allowed(href, self.src["domains"]):
                continue
            href = canonical_url(href)
            if href in known or not link_matches(cfg, self.src, href, text):
                continue
            known.add(href)
            new.append({"url": href, "source_id": self.src.id, "priority": entry["priority"],
                        "score": 0, "discovered_at": now_iso(), "title": text[:160], "kind": "pdf",
                        "depth": entry.get("depth", 0) + 1, "found_by": "fetch"})
        room = cap - len(read_jsonl(self.src.urls_path))
        if new and room > 0:
            append_jsonl(self.src.urls_path, new[:room])
            print(f"     + queued {len(new[:room])} linked PDF(s)")

    def fetch(self, entry: dict) -> dict:
        url = entry["url"]
        if entry.get("kind") != "postback" and looks_like_login(url, entry.get("title", "")) \
                and not allow_match(self.src, url, entry.get("title", "")):
            return {"status": "skipped_login"}
        if entry.get("kind") == "pdf" or is_pdf(url):
            return self.via_pdf(url)
        engine = self.engine
        if engine in ("firecrawl", "auto") and not self.firecrawl_exhausted and not entry.get("actions"):
            try:
                out = self.via_firecrawl(url)
                if len(out["md"]) >= MIN_CHARS or engine == "firecrawl":
                    return out
                print("     firecrawl returned almost nothing; trying local")
            except Exception as e:  # noqa: BLE001
                if engine == "firecrawl":
                    raise
                print(f"     firecrawl failed ({redact(type(e).__name__ + ': ' + str(e))[:120]}); trying local")
        elif engine == "firecrawl" and self.firecrawl_exhausted:
            raise RuntimeError("Firecrawl budget reached")
        return self.via_local(entry)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--engine", choices=["firecrawl", "local", "auto"])
    ap.add_argument("--preview", type=int, default=0, help="print the first N chars of each fetched page")
    args = ap.parse_args()

    src = get_source(args.source)
    engine = args.engine or src.engine
    f = Fetcher(src, engine)
    done = {m["url"] for m in read_jsonl(src.meta_path) if m.get("status") in DONE_STATUSES}
    print(f"== fetch {src.id} engine={engine} · ledger {f.budget.used}/{f.budget.limit} credits")

    fetched = 0
    counts: dict[str, int] = {}
    try:
        i = 0
        while True:
            # urls.jsonl can grow during the run (linked PDFs), so re-read each step
            todo = [e for e in read_jsonl(src.urls_path) if e["url"] not in done]
            if not todo or (args.limit is not None and fetched >= args.limit):
                break
            entry = todo[0]
            done.add(entry["url"])
            i += 1
            print(f"[{i}] p{entry['priority']} {entry['kind']:<8} {entry['url']}")
            row = {"url": entry["url"], "fetched_at": now_iso(), "source_id": src.id}
            try:
                out = f.fetch(entry)
            except Exception as e:  # noqa: BLE001
                out = {"status": "failed", "error": redact(f"{type(e).__name__}: {e}")[:300]}
                if f.firecrawl_exhausted and engine == "firecrawl":
                    print("  ! Firecrawl budget reached — stopping")
                    append_jsonl(src.meta_path, [{**row, **out}])
                    break
            md = out.pop("md", None)
            if md is not None:
                if len(md) < MIN_CHARS:
                    out["status"] = "empty"
                elif BLOCKED_RE.search(md):
                    out["status"] = "blocked_403"
                else:
                    path = src.dir / f"{sha1(entry['url'])}.md"
                    path.write_text(md + "\n", encoding="utf-8")
                    out.update(status="ok", file=path.name, chars=len(md), content_hash=sha1(md))
            row.update({"engine": None, "credits_used": 0, **out})
            append_jsonl(src.meta_path, [row])
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            fetched += 1
            extra = f" · {row.get('chars', 0)} chars" if row["status"] == "ok" else f" · {row.get('error', '')}"
            print(f"     {row['status']} via {row.get('engine')} · credits {row.get('credits_used', 0)}{extra}")
            if args.preview and md and row["status"] == "ok":
                print("     ---- preview ----")
                print("     " + md[: args.preview].replace("\n", "\n     "))
                print("     -----------------")
    finally:
        f.close()
    print(f"== {src.id}: {counts} · Firecrawl credits this run: {f.run_credits} · ledger {Budget().used}/{Budget().limit}")
    if counts.get("failed"):
        sys.exit(0)  # failures are recorded in meta.jsonl and retried on the next run


if __name__ == "__main__":
    main()
