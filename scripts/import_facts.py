"""Hand-collected facts (markdown) → overlay data/manual/service_details/<service_id>.json, merged by merge.py.

  .venv/bin/python scripts/import_facts.py --facts demo/facts_income_certificate.md --service gj_income_certificate

Facts file: `### <section>` headings, then lines `- <fact> · "<exact quote>" · <url>`.
Sections whose name starts with "document" become Documents; everything else becomes steps.
Each quote is checked verbatim (check.py normalisation) against the saved page for its URL:
  verbatim_ok           → accepted
  visual_transcription  → Gujarati quote, page is a PDF whose text layer is garbled (broken conjuncts
                          or a legacy non-Unicode font); accepted, but the
                          service becomes partially_verified with a note (needs a Gujarati reader)
  otherwise             → rejected and listed; the script exits non-zero
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urldefrag

from pipeline import (
    DATA, ROOT, domain_allowed, get_source, load_sources, normalise_text, now_iso, read_jsonl, write_json,
)
from extract import GUJARATI_RE, low_quality_reason
from shared.schemas import Document, Fact

LINE_RE = re.compile(r'^\s*-\s*(?P<value>.+?)\s*·\s*["“](?P<quote>.+)["”]\s*·\s*(?P<url>https?://\S+)\s*$')


def garbled_pdf(url: str, md: str, quote: str) -> bool:
    """PDF whose text layer can't contain this Gujarati quote: broken conjuncts (low_quality_reason)
    or a legacy (non-Unicode) Gujarati font that extracts as Latin gibberish."""
    if not url.lower().endswith(".pdf"):
        return False
    if low_quality_reason(url, md):
        return True
    gu_quote = GUJARATI_RE.search(quote) is not None
    gu_share = len(GUJARATI_RE.findall(md)) / (sum(c.isalpha() for c in md) or 1)
    return gu_quote and gu_share < 0.01


def page_index() -> dict[str, tuple[str, str]]:
    """url → (markdown, source id) for every ok fetch."""
    out = {}
    for sid, src in load_sources().items():
        for m in read_jsonl(src.meta_path):
            if m.get("status") == "ok":
                out[m["url"]] = ((src.dir / m["file"]).read_text(encoding="utf-8"), sid)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts", type=Path, required=True)
    ap.add_argument("--service", required=True)
    ap.add_argument("--note", default=None, help="note for visual transcriptions (default: 'visual transcription of <pdf> p.<n>')")
    args = ap.parse_args()

    pages = page_index()
    from pipeline import official_domains
    domains = official_domains()
    facts, rejected, section = [], [], "steps"
    for line in args.facts.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            section = line[4:].strip()
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        value, quote, url = m["value"].strip(), m["quote"].strip(), m["url"].strip()
        target = "documents" if section.lower().startswith("document") else "steps"
        page = pages.get(url) or pages.get(urldefrag(url)[0])
        entry = {"section": section, "target": target,
                 "fact": Fact(value=value, quote=quote, source_url=url).model_dump(mode="json")}
        if not domain_allowed(url, domains):
            rejected.append((value, "source_url not official"))
            continue
        if page and normalise_text(quote) in normalise_text(page[0]):
            entry["check"] = "verbatim_ok"
        elif page and garbled_pdf(urldefrag(url)[0], page[0], quote):
            pg = re.search(r"#page=(\d+)", url)
            entry["check"] = "visual_transcription"
            entry["note"] = args.note or f"visual transcription of {urldefrag(url)[0].rsplit('/', 1)[-1]}" + (f" p.{pg.group(1)}" if pg else "")
        else:
            rejected.append((value, "quote not found on the saved page" if page else "page not fetched"))
            continue
        facts.append(entry)

    if rejected:
        for v, why in rejected:
            print(f"  REJECTED {v[:70]} — {why}")
        sys.exit(f"{len(rejected)} fact(s) failed verification — fix the facts file, nothing written")

    visual = [f for f in facts if f["check"] == "visual_transcription"]
    notes = sorted({f["note"] for f in visual})
    overlay = {
        "service_id": args.service,
        "imported_from": str(args.facts.resolve().relative_to(ROOT)),
        "imported_at": now_iso(),
        "verification_status": "partially_verified" if visual else None,
        "note": "; ".join(notes) + " — a Gujarati reader should confirm the exact wording" if visual else None,
        "facts": facts,
    }
    # validate the shapes merge.py will build
    for f in facts:
        Fact.model_validate(f["fact"])
        if f["target"] == "documents":
            Document(name=f["fact"]["value"], fact=f["fact"])
    out = DATA / "manual" / "service_details" / f"{args.service}.json"
    write_json(out, overlay)
    print(f"wrote {out.relative_to(ROOT)} · {len(facts)} facts "
          f"({len(facts) - len(visual)} verbatim_ok, {len(visual)} visual_transcription)"
          + (f" · status {overlay['verification_status']}" if visual else ""))


if __name__ == "__main__":
    main()
