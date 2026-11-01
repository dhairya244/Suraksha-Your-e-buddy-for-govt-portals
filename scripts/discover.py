"""Find deep URLs for a source → data/raw/<source>/urls.jsonl.

  .venv/bin/python scripts/discover.py --source digital_gujarat
  .venv/bin/python scripts/discover.py --all

firecrawl sources: Firecrawl map (+ domain-limited search if thin). local sources: Playwright
crawl from the seeds (max depth 2, same domain). auto: Firecrawl map, local crawl if map fails.
"""
from __future__ import annotations

import argparse
import json
import re
import time

from firecrawl import RateLimitError

from pipeline import (
    Budget, LocalBrowser, SourceCfg, allow_match, canonical_url, domain_allowed, firecrawl_client, get_source, is_pdf,
    link_matches, load_config, load_sources, looks_like_login, match_score, now_iso, priority_for, read_jsonl,
    redact, slug, write_json, write_jsonl,
)

THIN = 15  # fewer kept URLs than this → also try Firecrawl search


def clean_title(text: str) -> str:
    text = re.sub(r"\bread more\b", "", text, flags=re.I)
    return re.sub(r"^\s*\d+\s+", "", text).strip()


class Collector:
    def __init__(self, src: SourceCfg, cfg: dict) -> None:
        self.src, self.cfg = src, cfg
        self.found: dict[str, dict] = {}

    def add(self, url: str, text: str, depth: int, *, kind: str | None = None,
            actions: list[dict] | None = None, seed: bool = False, label: str | None = None) -> bool:
        """`text` (title + description) decides keep/drop; `label` (title) decides priority."""
        label = text if label is None else label
        # postbacks share their parent page URL, so judge them by their own title only
        match_url = "" if kind == "postback" else url
        if not seed and not link_matches(self.cfg, self.src, match_url, text):
            return False
        if kind != "postback":
            url = canonical_url(url)
        if url in self.found:
            return False
        entry = {
            "url": url, "source_id": self.src.id,
            "priority": priority_for(self.src, url, ptext := (text if self.src.get("priority_uses_description") else label)),
            "score": match_score(self.src, url, ptext),
            "discovered_at": now_iso(), "title": clean_title(label)[:160],
            "kind": kind or ("pdf" if is_pdf(url) else "page"), "depth": depth,
        }
        if actions:
            entry["actions"] = actions
        self.found[url] = entry
        return True


def seed_list(src: SourceCfg) -> list[dict]:
    return [s if isinstance(s, dict) else {"url": s} for s in src["seeds"]]


def crawl_local(src: SourceCfg, col: Collector, seeds: list[str]) -> None:
    d = load_config()["defaults"]
    max_depth = src.get("max_depth", d["max_depth"])
    max_pages = src.get("max_crawl_pages", d["max_crawl_pages"])
    domains = src["domains"]
    pb_sel = src.get("postback_selector")

    with LocalBrowser() as b:
        if src.get("setup"):
            b.run_actions(src["setup"])
        queue = [(u, 0) for u in seeds]
        crawled: set[str] = set()
        while queue and len(crawled) < max_pages:
            url, depth = queue.pop(0)
            if url in crawled:
                continue
            if looks_like_login(url) or not b.allowed(url):
                print(f"  skip (login/robots) {url}")
                continue
            try:
                if b.page.url != url:  # after setup we may already be on the seed page
                    b.open(url)
            except Exception as e:  # noqa: BLE001
                print(f"  ! open failed {url}: {type(e).__name__}")
                continue
            crawled.add(url)
            if depth == 0:
                col.add(url, b.page.title(), 0, seed=True)
            for href, text, pb in b.links(pb_sel):
                if pb:
                    frag = slug(clean_title(text)) or slug(pb)
                    col.add(f"{url}#{frag}", text, depth + 1, kind="postback",
                            actions=[{"goto": url}, {"eval": pb, "wait_navigation": True}])
                    continue
                if not domain_allowed(href, domains) or (looks_like_login(href, text) and not allow_match(src, href, text)):
                    continue
                added = col.add(href, text, depth + 1)
                if added and not is_pdf(href) and depth + 1 < max_depth:
                    queue.append((href, depth + 1))
        print(f"  crawled {len(crawled)} pages")


def cached_map(src: SourceCfg, fc, budget: Budget, url: str, term: str | None, limit: int,
               refresh: bool) -> list[dict]:
    """Firecrawl map results, cached in data/raw/<source>/map_cache.json (re-filtering is free)."""
    cache_path = src.dir / "map_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    key = f"{url} | {term}"
    if key in cache and not refresh:
        return cache[key]
    if not budget.can_spend(1):
        raise RuntimeError("Firecrawl budget reached")
    for attempt in range(3):
        try:
            res = fc.map(url, search=term, limit=limit, include_subdomains=True, ignore_query_parameters=True)
            break
        except RateLimitError:
            if attempt == 2:
                raise
            print("  … Firecrawl rate limit (req/min); waiting 65 s")
            time.sleep(65)
    budget.spend(1, "map", src.id, key, estimated=True)
    cache[key] = [{"url": l.url, "title": l.title, "description": l.description} for l in res.links or []]
    write_json(cache_path, cache)
    return cache[key]


def discover_firecrawl(src: SourceCfg, col: Collector, refresh: bool = False) -> None:
    fc = firecrawl_client()
    budget = Budget()
    domains = src["domains"]
    max_urls = src.get("max_urls", load_config()["defaults"]["max_urls"])
    for seed in seed_list(src):
        terms = seed.get("map_search") or [None]
        for term in terms:
            try:
                links = cached_map(src, fc, budget, seed["url"], term, max(200, max_urls * 3), refresh)
            except Exception as e:  # noqa: BLE001
                print(f"  ! map failed {seed['url']} ({term}): {redact(f'{type(e).__name__}: {e}')[:200]}")
                if src.engine == "auto":
                    print(f"  → local crawl fallback for {seed['url']}")
                    crawl_local(src, col, [seed["url"]])
                break
            kept = 0
            for link in links:
                if domain_allowed(link["url"], domains):
                    text = " ".join(filter(None, [link["title"], link["description"]]))
                    kept += col.add(link["url"], text, 1, label=link["title"] or "")
            print(f"  map {seed['url']} search={term!r}: {len(links)} links, +{kept} kept")

    if len(col.found) < THIN and src.get("search_queries"):
        for q in src["search_queries"]:
            if not budget.can_spend(2):
                break
            try:
                res = fc.search(q, include_domains=domains, limit=10)
                budget.spend(2, "search", src.id, q, estimated=True)
            except Exception as e:  # noqa: BLE001
                print(f"  ! search failed {q!r}: {type(e).__name__}")
                continue
            kept = 0
            for r in res.web or []:
                url = getattr(r, "url", None)
                if url and domain_allowed(url, domains):
                    kept += col.add(url, " ".join(filter(None, [getattr(r, "title", ""), getattr(r, "description", "")])), 1,
                                    label=getattr(r, "title", "") or "")
            print(f"  search {q!r}: +{kept} kept")


def discover(sid: str, refresh: bool = False) -> None:
    src = get_source(sid)
    cfg = load_config()
    print(f"== {sid} (engine={src.engine})")
    col = Collector(src, cfg)
    if src.engine == "local":
        crawl_local(src, col, [s["url"] for s in seed_list(src)])
    else:
        discover_firecrawl(src, col, refresh)

    max_urls = src.get("max_urls", cfg["defaults"]["max_urls"])
    previous = {r["url"]: r for r in read_jsonl(src.urls_path)}
    rows = sorted(col.found.values(), key=lambda r: (r["priority"], -r["score"], r["depth"], r["url"]))[:max_urls]
    for r in rows:  # keep the first discovery time stable across re-runs
        if r["url"] in previous:
            r["discovered_at"] = previous[r["url"]]["discovered_at"]
    write_jsonl(src.urls_path, rows)

    by_p = {p: sum(r["priority"] == p for r in rows) for p in (1, 2, 3)}
    by_k = {k: sum(r["kind"] == k for r in rows) for k in ("page", "pdf", "postback")}
    fc_pages = by_k["page"] if src.engine in ("firecrawl", "auto") else 0
    print(f"  → {len(rows)} URLs (found {len(col.found)}, cap {max_urls}) · priority {by_p} · kinds {by_k}")
    print(f"  → est. fetch credits: ~{fc_pages} (Firecrawl HTML pages; PDFs are downloaded locally, 0 credits)")
    print(f"  → Firecrawl ledger: {Budget().used}/{Budget().limit} used")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--source")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="re-run Firecrawl map instead of using map_cache.json")
    args = ap.parse_args()
    for sid in (load_sources() if args.all else [args.source]):
        discover(sid, args.refresh)


if __name__ == "__main__":
    main()
