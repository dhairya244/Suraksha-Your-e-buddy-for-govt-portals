"""Verify extracted records → data/extracted/<source>/checked/*.json + check_log.jsonl.

  .venv/bin/python scripts/check.py --source digital_gujarat
  .venv/bin/python scripts/check.py --all [--no-http]

(a) every quote appears verbatim (whitespace/markdown-normalised) in its saved page, else Fact → unknown
(b) source_url / official_url on gov.in, nic.in or a known official domain, else Fact → unknown /
    record rejected
(c) official / tracking URLs answer HTTP < 400 (official_url falls back to the source page; tracking → null)
(d) every record validates against shared/schemas, else rejected
"""
from __future__ import annotations

import argparse
import json
from urllib.parse import urldefrag

import httpx

from pipeline import (
    BROWSER_UA, EXTRACTED, RAW, append_jsonl, domain_allowed, get_source, load_sources,
    normalise_text, now_iso, official_domains, write_json,
)
from shared.schemas import UNKNOWN, Fact, PortalProfile, Scheme, Service, ServiceDetails

URL_CACHE = EXTRACTED / "url_status.json"


class Checker:
    def __init__(self, http: bool) -> None:
        self.http = http
        self.domains = official_domains()
        self.url_status: dict[str, int | str] = json.loads(URL_CACHE.read_text()) if URL_CACHE.exists() else {}
        self.stats = {"records": 0, "facts_kept": 0, "facts_blanked": 0, "rejected": 0, "urls_fixed": 0}
        self.log: list[dict] = []

    # -- helpers
    def note(self, page: str, what: str, reason: str, **kw) -> None:
        self.log.append({"ts": now_iso(), "page": page, "what": what, "reason": reason, **kw})

    def official(self, url: str | None) -> bool:
        return bool(url) and url.startswith("http") and domain_allowed(url, self.domains)

    def reachable(self, url: str) -> bool:
        if not self.http:
            return True
        url = urldefrag(url)[0]
        if url not in self.url_status:
            status: int | str
            try:
                r = httpx.get(url, headers={"User-Agent": BROWSER_UA}, follow_redirects=True, timeout=20)
                status = r.status_code
            except httpx.ConnectError as e:
                # some gov sites send an incomplete TLS chain that browsers repair; status check only
                if "CERTIFICATE_VERIFY_FAILED" in str(e):
                    try:
                        status = httpx.get(url, headers={"User-Agent": BROWSER_UA}, follow_redirects=True,
                                           timeout=20, verify=False).status_code
                    except Exception as e2:  # noqa: BLE001
                        status = type(e2).__name__
                else:
                    status = type(e).__name__
            except Exception as e:  # noqa: BLE001
                status = type(e).__name__
            self.url_status[url] = status
        s = self.url_status[url]
        return isinstance(s, int) and s < 400

    def check_fact(self, f: dict, page_text: str, page: str, where: str) -> dict:
        fx = Fact.model_validate(f)
        if fx.is_unknown:
            return f
        reason = None
        if not self.official(fx.source_url):
            reason = "source_url_not_official"
        elif normalise_text(fx.quote) not in page_text:
            reason = "quote_not_found"
        if reason:
            self.stats["facts_blanked"] += 1
            self.note(page, where, reason, value=fx.value, quote=fx.quote[:200])
            return Fact.unknown().model_dump()
        self.stats["facts_kept"] += 1
        return f

    def check_facts(self, fs: list[dict], page_text: str, page: str, where: str) -> list[dict]:
        out = [self.check_fact(f, page_text, page, f"{where}[{i}]") for i, f in enumerate(fs)]
        return [f for f in out if f["value"] != UNKNOWN]

    def fix_url(self, url: str | None, fallback: str, page: str, where: str) -> str | None:
        if url and self.official(url) and self.reachable(url):
            return url
        if url != fallback:
            self.stats["urls_fixed"] += 1
            self.note(page, where, "url_not_official_or_unreachable", url=url,
                      status=self.url_status.get(urldefrag(url or "")[0]), replaced_with=fallback)
        return fallback

    # -- per record kind
    def service(self, rec: dict, text: str, page: str) -> dict | None:
        s, d = rec["service"], rec["details"]
        s["official_url"] = self.fix_url(s.get("official_url"), page, page, "service.official_url")
        if d.get("tracking_url") and not (self.official(d["tracking_url"]) and self.reachable(d["tracking_url"])):
            self.note(page, "details.tracking_url", "url_not_official_or_unreachable", url=d["tracking_url"])
            d["tracking_url"] = None
        for k in ("eligibility", "fees", "steps"):
            d[k] = self.check_facts(d[k], text, page, f"details.{k}")
        for k in ("timeline", "issuing_authority", "appeal"):
            d[k] = self.check_fact(d[k], text, page, f"details.{k}")
        docs = []
        for i, doc in enumerate(d["documents"]):
            doc["fact"] = self.check_fact(doc["fact"], text, page, f"details.documents[{i}]")
            if doc["fact"]["value"] != UNKNOWN:
                docs.append(doc)
        d["documents"] = docs
        Service.model_validate(s)
        ServiceDetails.model_validate(d)
        return {"service": s, "details": d}

    def portal(self, p: dict, text: str, page: str) -> dict:
        p["url"] = self.fix_url(p.get("url"), page, page, "portal.url")
        for k in ("registration_steps", "payment_methods"):
            p[k] = self.check_facts(p[k], text, page, f"portal.{k}")
        p["helpline"] = self.check_fact(p["helpline"], text, page, "portal.helpline")
        probs = []
        for i, c in enumerate(p["common_problems"]):
            c["fix"] = self.check_fact(c["fix"], text, page, f"portal.common_problems[{i}]")
            if c["fix"]["value"] != UNKNOWN:
                probs.append(c)
        p["common_problems"] = probs
        PortalProfile.model_validate(p)
        return p

    def scheme(self, rec: dict, text: str, page: str) -> dict:
        s, ev = rec["scheme"], rec["evidence"]
        for k in ("summary", "benefits", "how_to_apply", "needs_income_certificate"):
            ev[k] = self.check_fact(ev[k], text, page, f"scheme.{k}")
        for k in ("eligibility", "exclusions", "documents"):
            ev[k] = self.check_facts(ev[k], text, page, f"scheme.{k}")
        # rebuild the plain-string Scheme fields from the surviving evidence
        s["summary"] = ev["summary"]["value"]
        s["benefits"] = None if ev["benefits"]["value"] == UNKNOWN else ev["benefits"]["value"]
        s["how_to_apply"] = None if ev["how_to_apply"]["value"] == UNKNOWN else ev["how_to_apply"]["value"]
        for k in ("eligibility", "exclusions", "documents"):
            s[k] = [f["value"] for f in ev[k]]
        if ev["needs_income_certificate"]["value"] == UNKNOWN:
            s["required_service_ids"] = []
        s["official_url"] = self.fix_url(s.get("official_url"), page, page, "scheme.official_url")
        Scheme.model_validate(s)
        return {"scheme": s, "evidence": ev}

    # -- per page
    def page(self, sid: str, path) -> dict:
        doc = json.loads(path.read_text())
        page = doc["url"]
        md = (RAW / sid / doc["file"]).read_text(encoding="utf-8")
        text = normalise_text(md)
        if not self.official(page):
            self.note(page, "page", "source_url_not_official")
            n = len(doc["services"]) + len(doc["portals"]) + len(doc["schemes"])
            self.stats["records"] += n
            self.stats["rejected"] += n
            doc.update(services=[], portals=[], schemes=[])
            return doc
        for kind, fn in (("services", self.service), ("portals", self.portal), ("schemes", self.scheme)):
            kept = []
            for i, rec in enumerate(doc[kind]):
                self.stats["records"] += 1
                try:
                    kept.append(fn(rec, text, page))
                except Exception as e:  # noqa: BLE001 - schema-invalid → reject
                    self.stats["rejected"] += 1
                    self.note(page, f"{kind}[{i}]", "schema_invalid", error=str(e)[:300])
            doc[kind] = kept
        doc["checked_at"] = now_iso()
        return doc

    def save_cache(self) -> None:
        write_json(URL_CACHE, self.url_status)


def run(sid: str, checker: Checker) -> None:
    src_dir = EXTRACTED / sid
    files = sorted(src_dir.glob("*.json"))
    before = dict(checker.stats)
    for path in files:
        doc = checker.page(sid, path)
        write_json(src_dir / "checked" / path.name, doc)
    append_jsonl(src_dir / "check_log.jsonl", checker.log)
    checker.log = []
    delta = {k: checker.stats[k] - before[k] for k in checker.stats}
    print(f"== check {sid}: pages={len(files)} · {delta}")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--source")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--no-http", action="store_true", help="skip HTTP reachability checks (rule c)")
    args = ap.parse_args()
    if args.source:
        get_source(args.source)
    checker = Checker(http=not args.no_http)
    try:
        for sid in (load_sources() if args.all else [args.source]):
            if (EXTRACTED / sid).exists():
                run(sid, checker)
    finally:
        checker.save_cache()
    s = checker.stats
    print(f"== summary: records={s['records']} · facts kept={s['facts_kept']} · facts blanked={s['facts_blanked']} "
          f"· rejected={s['rejected']} · urls fixed={s['urls_fixed']}")


if __name__ == "__main__":
    main()
