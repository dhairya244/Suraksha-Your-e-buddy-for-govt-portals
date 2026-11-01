"""Hand-walk notes + Sugam manual → data/forms/gj_income_certificate.json (FormMap).

  .venv/bin/python scripts/build_form_map.py
  .venv/bin/python scripts/build_form_map.py --notes demo/handwalk_income_certificate.md --dry-run

Labels, order, type, required, options, format hints and error texts come ONLY from the
hand-walk notes (parsed deterministically). Gemini adds meaning, rules, documents, error fixes,
ask-backs, pre-submit checklist and after-submit info from the official manual/form/service page.
The script fails if any label differs from the notes or the result isn't a valid FormMap.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from pipeline import DATA, ROOT, env, get_source, read_jsonl, slug, write_json
from backend.core.llm import call_gemini
from shared.schemas import FormMap

PROCESS_ID = "gj_income_certificate"
DEFAULT_NOTES = ROOT / "demo" / "handwalk_income_certificate.md"
# official pages used as context (matched against data/raw/digital_gujarat/meta.jsonl URLs)
MANUAL_PATTERNS = [r"5_IncomeCertificate\(Sugam\)\.pdf", r"pdfforms/s799\.pdf", r"#income_certificate_sugam$"]
SENSITIVE_RE = re.compile(r"aadhaa?r|આધાર|bank|account|ifsc|otp|password|pan\b|card number", re.I)
PLACEHOLDER_RE = re.compile(r"^<.*>$")
TYPES = {"text", "number", "date", "dropdown", "radio", "checkbox", "file", "otp", "password"}


# ---------------------------------------------------------------- parse notes
def kv(lines: list[str]) -> dict[str, str]:
    out = {}
    for line in lines:
        m = re.match(r"^\s*-\s*([a-zA-Z_ ]+?)\s*:\s*(.*)$", line)
        if m:
            val = m.group(2).split("  (")[0].strip()
            out[m.group(1).strip().lower()] = "" if PLACEHOLDER_RE.match(val) else val
    return out


def split_list(s: str) -> list[str]:
    return [x.strip().strip('"“”') for x in s.split(";") if x.strip().strip('"“”')]


def parse_notes(path: Path) -> tuple[list[dict], dict]:
    text = path.read_text(encoding="utf-8")
    pages, after = [], {}
    for block in re.split(r"(?m)^## ", text)[1:]:
        head, *body = block.splitlines()
        if head.lower().startswith("after submit"):
            after = kv(body)
            continue
        if not head.startswith("Page:"):
            continue
        title = head[len("Page:"):].strip()
        if PLACEHOLDER_RE.match(title):
            continue
        field_blocks = re.split(r"(?m)^### Field:", "\n".join(body))
        pmeta = kv(field_blocks[0].splitlines())
        page = {"page_id": pmeta.get("page_id") or slug(title), "order": len(pages) + 1, "title": title,
                "recognise_by": split_list(pmeta.get("identifying phrases", "")),
                "is_login": pmeta.get("login page", "no").lower().startswith("y"), "fields": []}
        for fb in field_blocks[1:]:
            label, *flines = fb.splitlines()
            label = label.strip()
            if not label or PLACEHOLDER_RE.match(label):
                continue
            f = kv(flines)
            ftype = (f.get("type") or "text").lower()
            if ftype not in TYPES:
                sys.exit(f"field {label!r}: type {ftype!r} must be one of {sorted(TYPES)}")
            fid = f.get("field_id") or slug(label)
            page["fields"].append({
                "field_id": fid, "order": len(page["fields"]) + 1, "label": label,
                "label_aliases": [f["gujarati label"]] if f.get("gujarati label") else [],
                "type": ftype, "required": (f.get("required") or "").lower().startswith("y"),
                "sensitive": ftype in ("otp", "password") or bool(SENSITIVE_RE.search(label + " " + f.get("gujarati label", ""))),
                "options": split_list(f["options"]) if f.get("options") else None,
                "format_hint": f.get("format hint") or None,
                "_upload_rules": f.get("upload rules", ""), "_errors": split_list(f.get("errors seen", "")),
                "_notes": f.get("notes", ""),
            })
        pages.append(page)
    return pages, after


# ---------------------------------------------------------------- what Gemini adds
class XDoc(BaseModel):
    name: str
    formats: list[str]
    max_size_kb: int | None


class XError(BaseModel):
    message_contains: str = Field(description="Copy exactly one of the field's 'errors seen' texts.")
    fix: str


class XField(BaseModel):
    field_id: str
    meaning: str = Field(description="One plain sentence: what to enter here.")
    rules: list[str] = Field(description="Rules stated in the official text or notes; empty if none.")
    documents: list[XDoc]
    common_errors: list[XError]
    ask_back: str | None = Field(description="id of an ask_back this field depends on, or null.")
    from_official_text: bool = Field(description="true if meaning/rules come from the official text (not only the notes).")


class XAskBack(BaseModel):
    id: str
    question: str
    options: list[str]
    affects: list[str]


class XAfter(BaseModel):
    next_steps: str | None
    timeline: str | None
    fee: str | None
    tracking_url: str | None


class XEnrich(BaseModel):
    fields: list[XField]
    ask_backs: list[XAskBack]
    pre_submit_checklist: list[str]
    after_submit: XAfter


PROMPT = """You are building a field guide for the Gujarat Income Certificate (Sugam) online form.
Below are (1) hand-walk notes listing every field (authoritative for labels, types, options, errors)
and (2) official text from Digital Gujarat (user manual, offline form, service page).

For EVERY field_id in the notes return: meaning (one simple sentence), rules, documents (with
formats / max size only if stated), common_errors (one per 'errors seen' text, with a practical fix),
ask_back (id or null). Also return ask_backs (e.g. applying for self or a family member),
a short pre_submit_checklist, and after_submit (only facts stated in the text, else null).
Rules: use only the notes and the official text; never invent fees, documents, deadlines or rules;
keep official terms; copy Gujarati exactly. Note: the Gujarati PDF text layer may be garbled
(broken conjuncts) — never copy garbled Gujarati words into your output.

--- HAND-WALK NOTES ---
{notes}

--- OFFICIAL TEXT ---
{official}
"""


def official_context(urls_override: list[str] | None) -> tuple[str, str | None]:
    src = get_source("digital_gujarat")
    metas = [m for m in read_jsonl(src.meta_path) if m.get("status") == "ok"]
    parts, manual_url = [], None
    for pat in MANUAL_PATTERNS:
        for m in metas:
            if re.search(pat, m["url"]) and (not urls_override or m["url"] in urls_override):
                parts.append(f"[source: {m['url']}]\n{(src.dir / m['file']).read_text(encoding='utf-8')[:60000]}")
                manual_url = manual_url or m["url"]
    return "\n\n".join(parts), manual_url


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", type=Path, default=DEFAULT_NOTES)
    ap.add_argument("--dry-run", action="store_true", help="parse notes and show the skeleton only")
    args = ap.parse_args()

    pages, after_notes = parse_notes(args.notes)
    nfields = sum(len(p["fields"]) for p in pages)
    if not nfields:
        sys.exit(f"{args.notes} has no filled-in pages/fields yet — complete the hand-walk first.")
    print(f"notes: {len(pages)} pages, {nfields} fields")
    for p in pages:
        print(f"  {p['order']}. {p['title']} ({p['page_id']}): " + ", ".join(f["label"] for f in p["fields"]))
    if args.dry_run:
        return

    official, manual_url = official_context(None)
    if not official:
        sys.exit("no official text found — run fetch.py --source digital_gujarat first")
    res = call_gemini(PROMPT.format(notes=args.notes.read_text(encoding="utf-8"), official=official),
                      model=env("GEMINI_DATA_MODEL"), key="GEMINI_API_KEY_DATA", schema=XEnrich,
                      timeout_s=180, max_backoff_s=60)
    enrich: XEnrich = res.parsed
    by_id = {f.field_id: f for f in enrich.fields}

    missing = []
    for p in pages:
        for f in p["fields"]:
            x = by_id.get(f["field_id"])
            errors, upload, notes = f.pop("_errors"), f.pop("_upload_rules"), f.pop("_notes")
            if not x:
                missing.append(f["field_id"])
                f.update(meaning=notes or None, rules=[], documents=[], common_errors=[], ask_back=None, source_url=None)
                continue
            # error texts must be the hand-walk ones, verbatim
            fixes = {e.message_contains: e.fix for e in x.common_errors}
            f.update(
                meaning=x.meaning, rules=x.rules,
                documents=[d.model_dump() for d in x.documents],
                common_errors=[{"message_contains": e, "fix": fixes.get(e, "")} for e in errors if fixes.get(e)],
                ask_back=x.ask_back, source_url=manual_url if x.from_official_text else None,
            )
            if upload and not f["documents"]:
                f["rules"].append(f"Upload rules shown on the form: {upload}")
    after = enrich.after_submit.model_dump()
    for k_notes, k in (("next steps", "next_steps"), ("timeline", "timeline"), ("fee", "fee")):
        if after_notes.get(k_notes):
            after[k] = after_notes[k_notes]
    after["source_url"] = manual_url
    form = {"process_id": PROCESS_ID, "pages": pages, "ask_backs": [a.model_dump() for a in enrich.ask_backs],
            "pre_submit_checklist": enrich.pre_submit_checklist, "after_submit": after}

    fm = FormMap.model_validate(form)
    notes_labels = [(p["page_id"], f["label"]) for p in parse_notes(args.notes)[0] for f in p["fields"]]
    out_labels = [(p.page_id, f.label) for p in fm.pages for f in p.fields]
    if notes_labels != out_labels:
        sys.exit("label mismatch between notes and form map — refusing to write")
    out = DATA / "forms" / f"{PROCESS_ID}.json"
    write_json(out, fm.model_dump(mode="json"))
    print(f"wrote {out.relative_to(ROOT)} via {res.model} · {nfields} fields"
          + (f" · NOT enriched (no Gemini output): {missing}" if missing else ""))


if __name__ == "__main__":
    main()
