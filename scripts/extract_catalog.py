"""Sugam services listing (already fetched) → one Service record per listed service, no LLM.

  .venv/bin/python scripts/extract_catalog.py
  .venv/bin/python scripts/check.py --source digital_gujarat && .venv/bin/python scripts/merge.py

Parses the "##### <name>" headings of SugamDGAbhiyanServices.aspx (names copied exactly) into
data/extracted/digital_gujarat/zz_catalog.json in the extract.py format, so check.py and merge.py
treat it like any other page. Details stay unknown — only the names are on the listing page.
Department is filled only when the name states it; per-service Read More / Apply links are
ASP.NET postbacks, so official_url is the listing page itself.
"""
from __future__ import annotations

import re

from pipeline import EXTRACTED, get_source, load_config, now_iso, read_jsonl, slug, write_json
from shared.schemas import UNKNOWN, Fact, Service, ServiceDetails

LISTING_URL = "https://www.digitalgujarat.gov.in/Services/SugamDGAbhiyanServices.aspx"
OUT_NAME = "zz_catalog.json"  # merge.py reads it first: official names win, page details fill in
DEPT_RE = re.compile(r"(Directorate,?\s+(?:Scheduled|Developing) Caste Welfare|District Social Welfare Officer)")


def stable_id(name: str, rules: list[dict]) -> str:
    for r in rules:
        if r.get("state", "Gujarat").lower() == "gujarat" and re.search(r["pattern"], name, re.I):
            return r["id"]
    return "gj_" + slug(re.sub(r"\(\s*sugam\s*\)", "", name, flags=re.I), 80)


def category(name: str) -> str:
    n = name.lower()
    return ("ration_card" if "ration card" in n else "affidavit" if "affidavit" in n
            else "copy" if "copy" in n else "certificate")


def main() -> None:
    src = get_source("digital_gujarat")
    rules = load_config().get("service_ids", [])
    meta = next(m for m in reversed(read_jsonl(src.meta_path)) if m["url"] == LISTING_URL and m.get("status") == "ok")
    md = (src.dir / meta["file"]).read_text(encoding="utf-8")
    names = [n.strip() for n in re.findall(r"(?m)^#####\s+(.+)$", md) if n.strip() != "(Online)"]
    page_source_id = f"{src.id}_{meta['file'][:10]}"

    services, seen = [], set()
    for name in names:
        sid = stable_id(name, rules)
        if sid in seen:
            raise SystemExit(f"duplicate id {sid} for {name!r}")
        seen.add(sid)
        dept = DEPT_RE.search(name)
        s = Service(service_id=sid, name=name, alternate_names=[], department=dept.group(1) if dept else UNKNOWN,
                    government_level="state", state="Gujarat", category=category(name), application_mode="online",
                    official_url=LISTING_URL, portal_id="digital_gujarat", source_ids=[page_source_id])
        d = ServiceDetails(service_id=sid, eligibility=[], documents=[], fees=[], timeline=Fact.unknown(),
                           issuing_authority=Fact.unknown(), steps=[], tracking_url=None, appeal=Fact.unknown())
        services.append({"service": s.model_dump(mode="json"), "details": d.model_dump(mode="json")})

    write_json(EXTRACTED / src.id / OUT_NAME, {
        "url": LISTING_URL, "source_id": src.id, "page_source_id": page_source_id, "file": meta["file"],
        "content_hash": meta["content_hash"], "title": "Sugam Digital Gujarat", "fetched_at": meta["fetched_at"],
        "model": None, "extracted_at": now_iso(), "relevant": True, "services": services, "portals": [], "schemes": [],
    })
    print(f"== catalog: {len(services)} Sugam services → data/extracted/{src.id}/{OUT_NAME}")
    for x in services:
        print(f"   {x['service']['service_id']:<70} {x['service']['department'][:40]}")


if __name__ == "__main__":
    main()
