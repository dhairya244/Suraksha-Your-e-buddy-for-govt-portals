"""Spot-check merged facts against their sources, then record the verdict.

  .venv/bin/python scripts/review_sample.py --n 5                     # every service
  .venv/bin/python scripts/review_sample.py --id gj_income_certificate --n 10
  .venv/bin/python scripts/review_sample.py --id gj_income_certificate --mark verified --by P2
  .venv/bin/python scripts/review_sample.py --scheme pm_kisan --mark partially_verified --by P1

--mark sets verification_status (verified | partially_verified | pending | rejected) and appends
who/when to data/verification_log.jsonl.
"""
from __future__ import annotations

import argparse
import json
import random

from pipeline import DATA, append_jsonl, now_iso, write_json
from shared.schemas import UNKNOWN, VerificationStatus


def known_facts(d: dict) -> list[tuple[str, dict]]:
    out = []
    for k in ("eligibility", "fees", "steps"):
        out += [(k, f) for f in d[k]]
    for k in ("timeline", "issuing_authority", "appeal"):
        if d[k]["value"] != UNKNOWN:
            out.append((k, d[k]))
    out += [("documents", doc["fact"]) for doc in d["documents"]]
    return out


def show_service(sid: str, n: int, rng: random.Random) -> None:
    path = DATA / "service_details" / f"{sid}.json"
    if not path.exists():
        print(f"  (no details for {sid})")
        return
    facts = known_facts(json.loads(path.read_text()))
    print(f"\n== {sid} · {len(facts)} known facts · showing {min(n, len(facts))}")
    for field, f in rng.sample(facts, min(n, len(facts))):
        print(f"  [{field}] {f['value']}\n     quote: “{f['quote'][:300]}”\n     source: {f['source_url']}")


def show_scheme(s: dict) -> None:
    print(f"\n== scheme {s['scheme_id']} · {s['name']} ({s['level']}, {s.get('state') or 'all India'})")
    print(f"  summary: {s['summary'][:200]}\n  eligibility: {s['eligibility'][:3]}\n  source: {s['source_url']}")


def mark(kind: str, rid: str, status: str, by: str, note: str | None) -> None:
    path = DATA / ("services.json" if kind == "service" else "schemes.json")
    rows = json.loads(path.read_text())
    key = "service_id" if kind == "service" else "scheme_id"
    hit = next((r for r in rows if r[key] == rid), None)
    if not hit:
        raise SystemExit(f"{kind} {rid!r} not found in {path.name}")
    old = hit["verification_status"]
    hit["verification_status"] = status
    if kind == "service":
        hit["last_verified_at"] = now_iso()
    else:
        hit["verified_on"] = now_iso()[:10]
    write_json(path, rows)
    append_jsonl(DATA / "verification_log.jsonl", [{
        "ts": now_iso(), "action": "review", "kind": kind, "id": rid, "from": old, "status": status,
        "by": by, "note": note,
    }])
    print(f"{kind} {rid}: {old} → {status} (by {by})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="service_id (default: all services)")
    ap.add_argument("--scheme", help="scheme_id")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--mark", choices=[s.value for s in VerificationStatus])
    ap.add_argument("--by", help="who reviewed (required with --mark)")
    ap.add_argument("--note")
    args = ap.parse_args()

    if args.mark:
        if not args.by or not (args.id or args.scheme):
            raise SystemExit("--mark needs --by and one of --id / --scheme")
        mark("scheme" if args.scheme else "service", args.scheme or args.id, args.mark, args.by, args.note)
        return

    rng = random.Random(args.seed)
    if args.scheme:
        s = next((s for s in json.loads((DATA / "schemes.json").read_text()) if s["scheme_id"] == args.scheme), None)
        if not s:
            raise SystemExit(f"scheme {args.scheme!r} not found")
        show_scheme(s)
        return
    services = json.loads((DATA / "services.json").read_text())
    for s in services:
        if args.id and s["service_id"] != args.id:
            continue
        print(f"\n{s['name']} · {s['verification_status']}")
        show_service(s["service_id"], args.n, rng)


if __name__ == "__main__":
    main()
