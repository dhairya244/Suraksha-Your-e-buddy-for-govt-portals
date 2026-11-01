"""Merge checked records from all sources into the knowledge files the backend loads.

  .venv/bin/python scripts/merge.py

Writes data/services.json, data/service_details/<id>.json, data/portals/<id>.json,
data/schemes.json, data/sources.json and appends data/verification_log.jsonl.
Services get stable ids from `service_ids` in sources.yaml; others are deduped on
normalised name + state + department. New records are `pending`; a status already set by
review_sample.py is kept as long as the record's facts are unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urldefrag

from pipeline import (
    DATA, EXTRACTED, append_jsonl, host, is_pdf, load_config, load_sources, now_iso, read_jsonl,
    sha1, slug, write_json,
)
from shared.schemas import (
    UNKNOWN, PortalProfile, Scheme, Service, ServiceDetails, Source, VerificationStatus,
)

LOG = DATA / "verification_log.jsonl"


def norm(s: str | None) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


def fingerprint(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def service_id_for(s: dict, rules: list[dict]) -> str:
    hay = " ".join([s["name"], *s.get("alternate_names", [])])
    for r in rules:
        if r.get("state") and (s.get("state") or "").lower() != r["state"].lower():
            continue
        if re.search(r["pattern"], hay, re.I):
            return r["id"]
    prefix = "gj" if (s.get("state") or "").lower() == "gujarat" else "central" if s["government_level"] == "central" else slug(s.get("state") or "in")
    return f"{prefix}_{slug(s['name'], 50)}"


def portal_id_for(url: str, fallback: str, mapping: dict[str, str]) -> str:
    h = host(url)
    for dom, pid in mapping.items():
        if h == dom or h.endswith("." + dom):
            return pid
    return fallback


def union_facts(a: list[dict], b: list[dict]) -> list[dict]:
    """a + facts of b not already in a (same value or same quote)."""
    seen = {norm(f["value"]) for f in a} | {norm(f["quote"]) for f in a}
    out = list(a)
    for f in b:
        if norm(f["value"]) not in seen and norm(f["quote"]) not in seen:
            out.append(f)
            seen |= {norm(f["value"]), norm(f["quote"])}
    return out


def overlay_details(o: dict) -> dict:
    """data/manual/service_details/<id>.json (import_facts.py) → ServiceDetails-shaped dict."""
    d = ServiceDetails(service_id=o["service_id"]).model_dump(mode="json")
    for f in o["facts"]:
        if f["target"] == "documents":
            d["documents"].append({"name": f["fact"]["value"], "formats": [], "max_size_kb": None, "fact": f["fact"]})
        else:
            d[f["target"]].append(f["fact"])
    return d


def first_known(a: dict, b: dict) -> dict:
    return a if a["value"] != UNKNOWN else b


def merge_details(a: dict, b: dict) -> dict:
    for k in ("eligibility", "fees", "steps"):
        a[k] = union_facts(a[k], b[k])
    for k in ("timeline", "issuing_authority", "appeal"):
        a[k] = first_known(a[k], b[k])
    names = {norm(d["name"]) for d in a["documents"]}
    a["documents"] += [d for d in b["documents"] if norm(d["name"]) not in names and not names.add(norm(d["name"]))]
    a["tracking_url"] = a.get("tracking_url") or b.get("tracking_url")
    a["disclaimer"] = a.get("disclaimer") or b.get("disclaimer")
    return a


def load_previous(path, key: str) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {r[key]: r for r in json.loads(path.read_text())}


def main() -> None:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    cfg = load_config()
    sources = load_sources()
    rules = cfg.get("service_ids", [])
    portal_map = cfg.get("portal_ids", {})
    excluded = set((cfg.get("merge") or {}).get("exclude_service_ids", []))
    meta_by_url = {m["url"]: m for sid in sources for m in read_jsonl(sources[sid].meta_path) if m.get("status") == "ok"}

    services: dict[str, dict] = {}
    details: dict[str, dict] = {}
    portals: dict[str, dict] = {}
    schemes: dict[str, dict] = {}
    manual_sources: dict[str, dict] = {}
    used_pages: dict[str, tuple[str, dict]] = {}  # page_source_id → (source id, checked doc)
    order = list(sources)  # sources.yaml order = trust/priority order

    # catalog pages (extract_catalog.py) go first so official names win; their per-service
    # pages (<listing url>#<slug(name)>) map onto the catalog ids
    page_ids: dict[str, str] = {}
    for cat in EXTRACTED.glob("*/checked/zz_catalog.json"):
        c = json.loads(cat.read_text())
        page_ids.update({f"{c['url']}#{slug(r['service']['name'])}": r["service"]["service_id"] for r in c["services"]})

    for sid in order:
        for path in sorted((EXTRACTED / sid / "checked").glob("*.json"), key=lambda p: (p.name != "zz_catalog.json", p.name)):
            doc = json.loads(path.read_text())
            psid = doc["page_source_id"]
            for rec in doc["services"]:
                s, d = rec["service"], rec["details"]
                # catalog records arrive with stable ids already; LLM ones carry tmp_ ids
                if not s["service_id"].startswith("tmp_"):
                    sid_stable = s["service_id"]
                else:
                    sid_stable = page_ids.get(doc["url"]) or service_id_for(s, rules)
                if sid_stable in excluded:
                    continue
                s["service_id"] = d["service_id"] = sid_stable
                if sid_stable not in services:
                    services[sid_stable], details[sid_stable] = s, d
                else:
                    cur = services[sid_stable]
                    cur["alternate_names"] = list(dict.fromkeys(
                        cur["alternate_names"] + [n for n in [s["name"], *s["alternate_names"]] if norm(n) != norm(cur["name"])]))
                    cur["source_ids"] = list(dict.fromkeys(cur["source_ids"] + s["source_ids"]))
                    for k in ("application_mode", "department"):
                        if cur[k] == UNKNOWN:
                            cur[k] = s[k]
                    details[sid_stable] = merge_details(details[sid_stable], d)
                used_pages[psid] = (sid, doc)
            for p in doc["portals"]:
                pid = portal_id_for(doc["url"], p["portal_id"], portal_map)
                p["portal_id"] = pid
                if pid not in portals:
                    portals[pid] = p
                else:
                    cur = portals[pid]
                    cur["login_methods"] = list(dict.fromkeys(cur["login_methods"] + p["login_methods"]))
                    cur["registration_steps"] = union_facts(cur["registration_steps"], p["registration_steps"])
                    cur["payment_methods"] = union_facts(cur["payment_methods"], p["payment_methods"])
                    cur["helpline"] = first_known(cur["helpline"], p["helpline"])
                    probs = {norm(c["problem"]) for c in cur["common_problems"]}
                    cur["common_problems"] += [c for c in p["common_problems"] if norm(c["problem"]) not in probs]
                used_pages[psid] = (sid, doc)
            for rec in doc["schemes"]:
                s = rec["scheme"]
                url_slug = re.search(r"/schemes/([^/?#]+)", s["source_url"])
                scheme_id = slug(url_slug.group(1)) if url_slug else slug(s["name"], 50)
                key = f"{norm(s['name'])}|{norm(s.get('state'))}"
                existing = next((k for k, v in schemes.items() if f"{norm(v['name'])}|{norm(v.get('state'))}" == key), None)
                if existing:
                    cur = schemes[existing]
                    for k in ("eligibility", "exclusions", "documents", "tags", "required_service_ids"):
                        cur[k] = list(dict.fromkeys(cur[k] + s[k]))
                    continue
                s["scheme_id"] = scheme_id
                s["verified_on"] = None
                schemes[scheme_id] = s
                used_pages[psid] = (sid, doc)

    # hand-collected facts (import_facts.py) go first: human-checked beats scraped
    overlays: dict[str, dict] = {}
    for path in sorted((DATA / "manual" / "service_details").glob("*.json")):
        o = json.loads(path.read_text())
        if o["service_id"] not in services:
            print(f"  ! overlay {path.name}: service {o['service_id']} not in merged services — skipped")
            continue
        overlays[o["service_id"]] = o
        details[o["service_id"]] = merge_details(overlay_details(o), details[o["service_id"]])
        cur = services[o["service_id"]]
        # verbatim-checked facts point at pages already in sources.json; visual transcriptions get a manual Source
        for url in dict.fromkeys(urldefrag(f["fact"]["source_url"])[0] for f in o["facts"] if f.get("check") == "visual_transcription"):
            psid = f"manual_{sha1(url)[:10]}"
            visual = True
            manual_sources[psid] = Source(
                source_id=psid, name=url.rsplit("/", 1)[-1] + (" (visual transcription)" if visual else ""),
                official_url=url, government_level=cur["government_level"], state=cur.get("state"),
                department=None if cur["department"] == UNKNOWN else cur["department"],
                source_type="pdf" if is_pdf(url) else "portal", collection_method="manual",
                last_checked_at=datetime.fromisoformat(o["imported_at"]),
            ).model_dump(mode="json")
            if psid not in cur["source_ids"]:
                cur["source_ids"].append(psid)

    # search aliases from sources.yaml (names only, no facts)
    for sid_, names in (cfg.get("aliases") or {}).items():
        if sid_ in services:
            cur = services[sid_]
            have = {norm(n) for n in [cur["name"], *cur["alternate_names"]]}
            cur["alternate_names"] += [n for n in names if norm(n) not in have and not have.add(norm(n))]

    # services → source ids are page ids; build Source records for every page we used
    src_records = dict(manual_sources)
    for psid, (sid, doc) in used_pages.items():
        cfg_s = sources[sid]
        m = meta_by_url.get(doc["url"], {})
        url = doc["url"]
        stype = "pdf" if is_pdf(url) else "faq" if "faq" in url.lower() else "portal"
        method = "download" if stype == "pdf" else "scrape"
        src_records[psid] = Source(
            source_id=psid, name=(doc.get("title") or m.get("title") or cfg_s["name"])[:200], official_url=url,
            government_level=cfg_s["government_level"], state=cfg_s.get("state"), department=cfg_s.get("department"),
            source_type=stype, collection_method=method,
            last_checked_at=datetime.fromisoformat(doc.get("fetched_at") or now_iso()),
        ).model_dump(mode="json")

    # keep review statuses for unchanged records (fingerprints live in data/merge_state.json)
    state_path = DATA / "merge_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"services": {}, "schemes": {}}
    prev_services = load_previous(DATA / "services.json", "service_id")
    prev_schemes = load_previous(DATA / "schemes.json", "scheme_id")
    new_state = {"services": {}, "schemes": {}}
    log = []
    for sid_, s in services.items():
        fp = fingerprint(details[sid_])
        prev = prev_services.get(sid_)
        unchanged = state["services"].get(sid_) == fp
        if prev and unchanged and prev["verification_status"] != "pending":
            s["verification_status"], s["last_verified_at"] = prev["verification_status"], prev.get("last_verified_at")
        else:
            s["verification_status"], s["last_verified_at"] = VerificationStatus.pending.value, None
            if not unchanged:
                log.append({"ts": now_iso(), "action": "merge", "kind": "service", "id": sid_, "status": "pending"})
        o = overlays.get(sid_)
        if o and o.get("verification_status") and s["verification_status"] == VerificationStatus.pending.value:
            s["verification_status"], s["last_verified_at"] = o["verification_status"], o["imported_at"]
            if not prev or prev["verification_status"] != o["verification_status"]:
                log.append({"ts": now_iso(), "action": "manual_overlay", "kind": "service", "id": sid_,
                            "status": o["verification_status"], "by": "import_facts.py",
                            "source": o["imported_from"], "note": o.get("note")})
        new_state["services"][sid_] = fp
    for k, s in schemes.items():
        fp = fingerprint({x: s[x] for x in ("eligibility", "exclusions", "documents", "benefits", "summary")})
        prev = prev_schemes.get(k)
        unchanged = state["schemes"].get(k) == fp
        if prev and unchanged and prev["verification_status"] != "pending":
            s["verification_status"], s["verified_on"] = prev["verification_status"], prev.get("verified_on")
        else:
            s["verification_status"] = VerificationStatus.pending.value
            if not unchanged:
                log.append({"ts": now_iso(), "action": "merge", "kind": "scheme", "id": k, "status": "pending"})
        new_state["schemes"][k] = fp

    # validate everything against shared/schemas before writing
    for s in services.values():
        Service.model_validate(s)
    for d in details.values():
        ServiceDetails.model_validate(d)
    for p in portals.values():
        PortalProfile.model_validate(p)
    for s in schemes.values():
        Scheme.model_validate(s)

    write_json(state_path, new_state)
    write_json(DATA / "services.json", sorted(services.values(), key=lambda s: s["service_id"]))
    for sid_, d in details.items():
        write_json(DATA / "service_details" / f"{sid_}.json", d)
    for pid, p in portals.items():
        write_json(DATA / "portals" / f"{pid}.json", p)
    write_json(DATA / "schemes.json", sorted(schemes.values(), key=lambda s: s["scheme_id"]))
    write_json(DATA / "sources.json", sorted(src_records.values(), key=lambda s: s["source_id"]))
    append_jsonl(LOG, log)

    nfacts = sum(len(d["eligibility"]) + len(d["fees"]) + len(d["steps"]) + len(d["documents"])
                 + sum(d[k]["value"] != UNKNOWN for k in ("timeline", "issuing_authority", "appeal")) for d in details.values())
    print(f"== merge: services={len(services)} ({nfacts} facts) · portals={len(portals)} · schemes={len(schemes)} "
          f"· sources={len(src_records)} · log +{len(log)}")
    for sid_ in sorted(services):
        print(f"   {sid_:<38} {services[sid_]['verification_status']:<10} {services[sid_]['name'][:60]}")


if __name__ == "__main__":
    main()
