"""Extract structured records from fetched pages → data/extracted/<source>/<sha1(url)>.json.

  .venv/bin/python scripts/extract.py --source digital_gujarat [--limit N] [--force]

Uses GEMINI_DATA_MODEL on GEMINI_API_KEY_DATA via backend/core/llm.py. Gemini fills a simplified
schema (facts = {value, quote}); this script adds source_url / ids / timestamps and validates the
result against shared/schemas. Pages whose content_hash was already extracted are skipped.
"""
from __future__ import annotations

import argparse
import re
import time
from typing import Literal

from pydantic import BaseModel, Field

from pipeline import (
    EXTRACTED, SourceCfg, env, env_float, get_source, load_config, now_iso, read_jsonl, redact,
    write_json,
)
from backend.core.llm import LLMError, call_gemini
from shared.schemas import (
    UNKNOWN, CommonProblem, Document, Fact, PortalProfile, Scheme, Service, ServiceDetails,
)
from shared.schemas.scheme import SchemeTag

MAX_INPUT_CHARS = 150_000
EXTRACT_TIMEOUT_S = 90  # long pages need more than the app's GEMINI_TIMEOUT_SECONDS
GUJARATI_RE = re.compile(r"[\u0A80-\u0AFF]")


def low_quality_reason(url: str, md: str) -> str | None:
    """pypdf breaks Gujarati conjuncts (ગુજરાત → ગુિરાત), so Gujarati PDF text can't be quoted."""
    if not url.lower().split("#")[0].endswith(".pdf"):
        return None
    letters = sum(c.isalpha() for c in md) or 1
    return "garbled_gujarati_pdf_text" if len(GUJARATI_RE.findall(md)) / letters > 0.2 else None
KEY = "GEMINI_API_KEY_DATA"


# ---------------------------------------------------------------- what Gemini fills
class XFact(BaseModel):
    value: str = Field(description='The fact in short plain words, or "unknown".')
    quote: str = Field(description="Exact verbatim text copied from the page that states this fact; empty if unknown.")


class XDocument(BaseModel):
    name: str
    formats: list[str] = Field(description='File formats if stated, e.g. ["pdf", "jpg"]; else empty.')
    max_size_kb: int | None = Field(description="Max upload size in KB if stated, else null.")
    quote: str = Field(description="Verbatim text stating this document is required.")


class XServiceDetails(BaseModel):
    eligibility: list[XFact]
    documents: list[XDocument]
    fees: list[XFact]
    timeline: XFact
    issuing_authority: XFact
    steps: list[XFact] = Field(description="Application steps in order, each with its own quote.")
    tracking_url: str | None = Field(description="URL to track application status, only if on the page.")
    appeal: XFact


class XService(BaseModel):
    name: str = Field(description="Official service name exactly as on the page (English if available).")
    alternate_names: list[str] = Field(description="Other official names on the page, e.g. Gujarati/Hindi, copied exactly.")
    department: str
    government_level: Literal["central", "state", "district", "local"]
    state: str | None
    category: str = Field(description="One word, e.g. certificate, pan, licence, tax_return.")
    application_mode: Literal["online", "offline", "both", "unknown"]
    official_url: str | None = Field(description="Official URL to apply or read about the service, only if on the page.")
    details: XServiceDetails


class XProblem(BaseModel):
    problem: str
    fix: XFact


class XPortal(BaseModel):
    name: str
    url: str | None
    login_methods: list[str]
    registration_steps: list[XFact]
    common_problems: list[XProblem]
    helpline: XFact
    payment_methods: list[XFact]


class XScheme(BaseModel):
    name: str
    level: Literal["central", "state"]
    state: str | None
    summary: XFact
    eligibility: list[XFact]
    exclusions: list[XFact]
    benefits: XFact
    documents: list[XFact]
    how_to_apply: XFact
    official_url: str | None = Field(description="Official application/department URL if on the page.")
    tags: list[SchemeTag]
    needs_income_certificate: XFact = Field(description='value "yes" with the quote if the page lists an income certificate as required, else "unknown".')


class PageExtract(BaseModel):
    relevant: bool = Field(description="false if the page has no usable facts for the requested record kinds.")
    services: list[XService]
    portal: XPortal | None
    schemes: list[XScheme]


SYSTEM = """You extract facts about Indian government services from ONE official web page into JSON.
Rules (follow exactly):
- Use ONLY facts written on this page. Never use outside knowledge. Never guess.
- Every fact has a `quote`: text copied EXACTLY, character for character, from the page (a sentence or
  phrase, max ~300 chars). Do not paraphrase, translate, fix spelling or join separate sentences in a quote.
- If the page does not state something, set value "unknown" and quote "".
- Keep official terms exactly as written. Copy Gujarati and Hindi text exactly; never translate it.
- Ignore site boilerplate: menus, login/sign-out dialogs, "Something went wrong" messages, cookie
  banners, footers, calendars, visitor counters, social links.
- Only produce the record kinds requested; leave the others empty (portal = null).
- A page about one service should yield one service record, not one per section.
"""


def build_prompt(src: SourceCfg, kinds: list[str], url: str, title: str, text: str) -> str:
    want = {
        "service": "services: government services described on the page (process: eligibility, documents, fees, timeline, issuing authority, steps, tracking, appeal)",
        "portal": "portal: the web portal itself (login methods, registration steps, common problems + fixes, helpline, payment methods) — only if the page covers these",
        "scheme": "schemes: welfare schemes/scholarships (eligibility, exclusions, benefits, documents, how to apply)",
    }
    lines = [f"- {want[k]}" for k in kinds]
    extra = ""
    if src.get("disclaimer"):
        extra = f"\nThis is {src['name']}: process guidance only, not tax advice. Do not compute taxes."
    return (f"Source: {src['name']} ({src.get('state') or 'India'})\nPage URL: {url}\nPage title: {title}\n"
            f"Record kinds requested:\n" + "\n".join(lines) + extra +
            f"\n\n--- PAGE TEXT START ---\n{text}\n--- PAGE TEXT END ---")


def strip_boilerplate(src: SourceCfg, md: str) -> str:
    pats = (load_config().get("extract", {}).get("strip_lines", {}) or {}).get(src.id, [])
    if not pats:
        return md
    rx = [re.compile(p, re.I) for p in pats]
    return "\n".join(l for l in md.splitlines() if not any(r.search(l.strip()) for r in rx))


# ---------------------------------------------------------------- convert to shared schemas
def fact(x: XFact, url: str) -> Fact:
    if not x or x.value.strip().lower() == UNKNOWN or not x.quote.strip():
        return Fact.unknown()
    return Fact(value=x.value.strip(), quote=x.quote.strip(), source_url=url)


def facts(xs: list[XFact], url: str) -> list[Fact]:
    return [f for f in (fact(x, url) for x in xs) if not f.is_unknown]


def to_records(src: SourceCfg, px: PageExtract, url: str, page_source_id: str) -> dict:
    out = {"services": [], "portals": [], "schemes": []}
    for s in px.services:
        d = s.details
        service = Service(
            service_id=f"tmp_{page_source_id}_{len(out['services'])}",  # merge.py assigns stable ids
            name=s.name, alternate_names=s.alternate_names, department=s.department or src["department"],
            government_level=s.government_level, state=s.state or src.get("state"), category=s.category,
            application_mode=s.application_mode, official_url=s.official_url or url,
            portal_id=src.get("portal_id"), source_ids=[page_source_id],
        )
        details = ServiceDetails(
            service_id=service.service_id,
            eligibility=facts(d.eligibility, url),
            documents=[Document(name=x.name, formats=x.formats, max_size_kb=x.max_size_kb,
                                fact=fact(XFact(value=x.name, quote=x.quote), url)) for x in d.documents],
            fees=facts(d.fees, url), timeline=fact(d.timeline, url),
            issuing_authority=fact(d.issuing_authority, url), steps=facts(d.steps, url),
            tracking_url=d.tracking_url, appeal=fact(d.appeal, url), disclaimer=src.get("disclaimer"),
        )
        out["services"].append({"service": service.model_dump(mode="json"), "details": details.model_dump(mode="json")})
    if px.portal:
        p = px.portal
        portal = PortalProfile(
            portal_id=src.get("portal_id") or "unknown", name=p.name, url=p.url or url,
            login_methods=p.login_methods, registration_steps=facts(p.registration_steps, url),
            common_problems=[CommonProblem(problem=c.problem, fix=fact(c.fix, url)) for c in p.common_problems],
            helpline=fact(p.helpline, url), payment_methods=facts(p.payment_methods, url),
        )
        out["portals"].append(portal.model_dump(mode="json"))
    for s in px.schemes:
        ev = {
            "summary": fact(s.summary, url), "eligibility": facts(s.eligibility, url),
            "exclusions": facts(s.exclusions, url), "benefits": fact(s.benefits, url),
            "documents": facts(s.documents, url), "how_to_apply": fact(s.how_to_apply, url),
            "needs_income_certificate": fact(s.needs_income_certificate, url),
        }
        scheme = Scheme(
            scheme_id=f"tmp_{page_source_id}_{len(out['schemes'])}", name=s.name, level=s.level,
            state=s.state, summary=ev["summary"].value if not ev["summary"].is_unknown else UNKNOWN,
            eligibility=[f.value for f in ev["eligibility"]], exclusions=[f.value for f in ev["exclusions"]],
            benefits=None if ev["benefits"].is_unknown else ev["benefits"].value,
            documents=[f.value for f in ev["documents"]],
            how_to_apply=None if ev["how_to_apply"].is_unknown else ev["how_to_apply"].value,
            official_url=s.official_url or url, source_url=url, tags=s.tags,
            required_service_ids=["gj_income_certificate"] if not ev["needs_income_certificate"].is_unknown else [],
        )
        # evidence = the sourced facts behind the plain-string Scheme fields (check.py verifies them)
        out["schemes"].append({"scheme": scheme.model_dump(mode="json"),
                               "evidence": {k: (v.model_dump() if isinstance(v, Fact) else [f.model_dump() for f in v])
                                            for k, v in ev.items()}})
    return out


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true", help="re-extract even if content_hash is unchanged")
    args = ap.parse_args()

    src = get_source(args.source)
    model = env("GEMINI_DATA_MODEL")
    kinds = load_config()["extract"]["kinds"][src.id]
    interval = 60.0 / env_float("GEMINI_DATA_RPM", 10)
    out_dir = EXTRACTED / src.id

    # newest ok fetch per URL
    pages: dict[str, dict] = {}
    for m in read_jsonl(src.meta_path):
        if m.get("status") == "ok":
            pages[m["url"]] = m
    print(f"== extract {src.id} · {len(pages)} pages · model={model} · kinds={kinds} · ≤{env_float('GEMINI_DATA_RPM', 10):g} RPM")

    done = n = 0
    counts = {"services": 0, "portals": 0, "schemes": 0, "irrelevant": 0, "failed": 0, "skipped": 0, "low_quality": 0}
    last_call = 0.0
    for url, m in pages.items():
        if args.limit is not None and n >= args.limit:
            break
        out_path = out_dir / f"{m['file'][:-3]}.json"
        if out_path.exists() and not args.force:
            import json
            if json.loads(out_path.read_text()).get("content_hash") == m["content_hash"]:
                counts["skipped"] += 1
                continue
        n += 1
        md = (src.dir / m["file"]).read_text(encoding="utf-8")
        text = strip_boilerplate(src, md)[:MAX_INPUT_CHARS]
        page_source_id = f"{src.id}_{m['file'][:10]}"
        base = {"url": url, "source_id": src.id, "page_source_id": page_source_id, "file": m["file"],
                "content_hash": m["content_hash"], "title": m.get("title", ""), "fetched_at": m["fetched_at"]}
        if reason := low_quality_reason(url, md):
            write_json(out_path, {**base, "model": None, "extracted_at": now_iso(), "relevant": False,
                                  "low_quality": reason, "services": [], "portals": [], "schemes": []})
            counts["low_quality"] += 1
            print(f"  [{n}] low-quality source ({reason}) · not extracted · {url[-80:]}")
            continue
        wait = last_call + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        last_call = time.monotonic()
        try:
            res = call_gemini(build_prompt(src, kinds, url, m.get("title", ""), text), model=model, key=KEY,
                              schema=PageExtract, system_instruction=SYSTEM, timeout_s=EXTRACT_TIMEOUT_S, max_backoff_s=60)
            px: PageExtract = res.parsed
            records = to_records(src, px, url, page_source_id) if px.relevant else {"services": [], "portals": [], "schemes": []}
        except (LLMError, ValueError) as e:
            counts["failed"] += 1
            print(f"  ! {url}: {redact(str(e))[:200]}")
            continue
        write_json(out_path, {
            **base, "model": res.model, "extracted_at": now_iso(), "relevant": px.relevant, **records,
        })
        done += 1
        for k in ("services", "portals", "schemes"):
            counts[k] += len(records[k])
        if not px.relevant:
            counts["irrelevant"] += 1
        print(f"  [{n}] {res.model} · services={len(records['services'])} portals={len(records['portals'])} "
              f"schemes={len(records['schemes'])} · {url[-80:]}")
    print(f"== {src.id}: extracted {done} pages · {counts}")


if __name__ == "__main__":
    main()
