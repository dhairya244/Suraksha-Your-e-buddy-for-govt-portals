"""Knowledge store: data/*.json loaded in memory, plus a cheap keyword picker for answer context.

Picking is deliberately simple (aliases in English, Hindi, Gujarati + service names): the demo form's
service and portal are always included, other services/schemes only when the question names them.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from backend.config import ROOT

DATA = ROOT / "data"
DEFAULT_SERVICE = "gj_income_certificate"
DEFAULT_PORTAL = "digital_gujarat"
MAX_SERVICES = 3

# topic → regex over the (lower-cased) question; matched against service ids/names
TOPICS: dict[str, str] = {
    "pan": r"\bpan\b|पैन|પાન|e-?pan",
    "itr": r"\bitr\b|income ?tax|tax return|रिटर्न|आयकर|ઇન્કમ ટેક્સ|રિટર્ન",
    "driving_licence": r"licen[cs]e|\bdl\b|लाइसेंस|लायसेंस|લાઇસન્સ|ड्राइविंग|ડ્રાઇવિંગ",
    "ration_card": r"ration|राशन|રેશન",
    "caste": r"caste|\bsc\b|\bst\b|sebc|obc|जाति|જાતિ",
    "ews": r"\bews\b|economically weaker|आर्थिक रूप से कमजोर",
    "non_creamy": r"creamy|क्रीमी",
    "income_certificate": r"income certificate|आय प्रमाण|आवक|aavak|aay praman|આવક",
}
SCHEME_RE = re.compile(r"scheme|yojana|योजना|યોજના|subsidy|सब्सिडी|सहायता|benefit|लाभ|sahay", re.I)


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


@lru_cache(maxsize=1)
def services() -> dict[str, dict]:
    return {s["service_id"]: s for s in (_load(DATA / "services.json") or [])}


@lru_cache(maxsize=64)
def service_details(service_id: str) -> dict | None:
    return _load(DATA / "service_details" / f"{service_id}.json")


@lru_cache(maxsize=8)
def portal(portal_id: str) -> dict | None:
    return _load(DATA / "portals" / f"{portal_id}.json")


@lru_cache(maxsize=1)
def schemes() -> list[dict]:
    return _load(DATA / "schemes.json") or []


def _service_matches(topic: str, sid: str, svc: dict) -> bool:
    hay = f"{sid} {svc.get('name', '')} {' '.join(svc.get('alternate_names', []))}".lower()
    return {
        "pan": "pan" in sid,
        "itr": svc.get("category") == "tax_return" and "pan" not in sid,
        "driving_licence": "licen" in hay or "driving" in hay,
        "ration_card": "ration" in hay,
        "caste": "caste" in hay and "eligibility" not in hay,
        "ews": "economically weaker" in hay,
        "non_creamy": "creamy" in hay,
        "income_certificate": sid.startswith("gj_income_certificate"),
    }.get(topic, False)


def pick_context(question: str, current_service: str = DEFAULT_SERVICE) -> dict:
    """Compact JSON-able context for one question."""
    q = question.lower()
    topics = [t for t, rx in TOPICS.items() if re.search(rx, q, re.I)]
    ids: list[str] = [current_service]
    for t in topics:
        for sid, svc in services().items():
            if _service_matches(t, sid, svc) and sid not in ids:
                ids.append(sid)
    ids = ids[:MAX_SERVICES]

    ctx: dict = {"services": []}
    portals_needed = {DEFAULT_PORTAL}
    for sid in ids:
        svc = services().get(sid, {"service_id": sid})
        det = service_details(sid)
        ctx["services"].append({"service": svc, "details": det})
        if svc.get("portal_id"):
            portals_needed.add(svc["portal_id"])
    ctx["portals"] = [p for p in (portal(pid) for pid in sorted(portals_needed)) if p]

    if SCHEME_RE.search(question):
        ctx["schemes"] = [
            {k: s.get(k) for k in ("scheme_id", "name", "level", "state", "summary", "eligibility", "documents", "official_url", "source_url") if s.get(k) is not None}
            for s in schemes()  # ~30 schemes, compact fields only
        ]
    return ctx
