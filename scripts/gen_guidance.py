"""Pre-generate field guidance (PLAYBOOK §7.5, prompt §13.3) → data/guidance/<process_id>/<lang>.json.

  .venv/bin/python scripts/gen_guidance.py --process gj_income_certificate [--langs hi,en]

One Gemini call per language covering every field of data/forms/<process_id>.json. Grounded only in
the form map and data/service_details/<process_id>.json; every `sources` entry must be one of the
URLs supplied, else it is dropped. Entries are written with reviewed=false — a speaker of each
language reviews before the demo.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from pydantic import BaseModel, Field

from pipeline import DATA, ROOT, env, write_json
from backend.core.llm import call_gemini
from shared.schemas import UNKNOWN, FormMap, GuidanceFile, ServiceDetails

LANG_NAMES = {"hi": "Hindi (Devanagari script)", "en": "simple Indian English", "gu": "Gujarati (Gujarati script)"}


class XGuidance(BaseModel):
    field_id: str
    say: str = Field(description="1–2 short spoken sentences.")
    sources: list[str] = Field(description="URLs (from the ones given) that support what is said; empty if only the form itself.")


class XGuidanceSet(BaseModel):
    items: list[XGuidance]


PROMPT = """You write what a voice assistant says when the user reaches each field of a government form.
Process: {process}. Situation: field_change (no user utterance).
Language: {lang_name}.

For EVERY field below write `say`: 1–2 short, warm, spoken sentences that explain what to enter and the
key rule (format, required, upload limits) if there is one.
Rules:
- Use ONLY the field JSON and the official facts below. Never add fees, documents, deadlines or rules
  that are not written there. If unsure, just say what the field asks for.
- Say the field label exactly as it appears on screen (quote it in its original language/script) so the
  user can find it; explain the rest in {lang_name}.
- Sensitive fields (sensitive=true): tell the user to type it themselves; never ask them to say it aloud.
- Do not read out or ask for personal values. Simple words, no jargon, no markdown.
- `sources`: copy the URL(s) of the facts you used, only from the URLs given; empty if you used only the form.

--- FIELDS ---
{fields}

--- OFFICIAL FACTS (value · quote · source_url) ---
{facts}
"""


def facts_text(d: ServiceDetails) -> tuple[str, set[str]]:
    rows, urls = [], set()
    def add(kind, f):
        if f.value != UNKNOWN:
            rows.append(f"[{kind}] {f.value} · “{f.quote}” · {f.source_url}")
            urls.add(str(f.source_url))
    for k in ("eligibility", "fees", "steps"):
        for f in getattr(d, k):
            add(k, f)
    for doc in d.documents:
        add("document", doc.fact)
    for k in ("timeline", "issuing_authority", "appeal"):
        add(k, getattr(d, k))
    return "\n".join(rows) or "(none)", urls


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--process", default="gj_income_certificate")
    ap.add_argument("--langs", default=env("DEMO_LANGS", "hi,en"))
    args = ap.parse_args()

    form_path = DATA / "forms" / f"{args.process}.json"
    if not form_path.exists():
        sys.exit(f"{form_path.relative_to(ROOT)} missing — run build_form_map.py first")
    fm = FormMap.model_validate_json(form_path.read_text())
    details_path = DATA / "service_details" / f"{args.process}.json"
    d = ServiceDetails.model_validate_json(details_path.read_text()) if details_path.exists() else ServiceDetails(service_id=args.process)
    facts, allowed = facts_text(d)
    fields = [f for p in fm.pages for f in p.fields]
    allowed |= {str(f.source_url) for f in fields if f.source_url}
    field_json = json.dumps([{"page": p.title, **f.model_dump(mode="json", exclude_none=True)}
                             for p in fm.pages for f in p.fields], ensure_ascii=False, indent=1)
    sources_of = {f.field_id: [str(f.source_url)] if f.source_url else [] for f in fields}

    for lang in [x.strip() for x in args.langs.split(",") if x.strip()]:
        res = call_gemini(PROMPT.format(process=args.process, lang_name=LANG_NAMES.get(lang, lang),
                                        fields=field_json, facts=facts),
                          model=env("GEMINI_DATA_MODEL"), key="GEMINI_API_KEY_DATA", schema=XGuidanceSet,
                          timeout_s=120, temperature=0.3)
        by_id = {g.field_id: g for g in res.parsed.items}
        out, missing, long_ = {}, [], []
        for f in fields:
            g = by_id.get(f.field_id)
            if not g or not g.say.strip():
                missing.append(f.field_id)
                continue
            if len([s for s in re.split(r"[.!?।]+", g.say) if s.strip()]) > 2:
                long_.append(f.field_id)
            srcs = [u for u in g.sources if u in allowed] or sources_of[f.field_id]
            out[f.field_id] = {"say": g.say.strip(), "sources": list(dict.fromkeys(srcs)), "reviewed": False}
        GuidanceFile.model_validate(out)
        path = DATA / "guidance" / args.process / f"{lang}.json"
        write_json(path, out)
        print(f"wrote {path.relative_to(ROOT)} via {res.model} · {len(out)}/{len(fields)} fields"
              + (f" · MISSING {missing}" if missing else "") + (f" · >2 sentences: {long_}" if long_ else ""))


if __name__ == "__main__":
    main()
