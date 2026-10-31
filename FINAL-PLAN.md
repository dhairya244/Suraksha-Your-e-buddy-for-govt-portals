# Suraksha — Final MVP Plan (Gujarat)

> **This plan overrides scope and timing in PLAYBOOK.md.** Use the playbook only for technical details (architecture §3, contracts §7–9, build steps §10–12, prompts §13, gotchas §17).
> **Assumed deadline:** Sunday 1 Nov ~20:00 (27 h from Sat 17:12). If your real deadline differs, shift every time below by the same amount.

---

## 1. What the MVP is (locked)

**Must ship**
1. Floating overlay (Windows + macOS) that watches the browser after "guide me" — no hotkey
2. **Gujarat Income Certificate (Sugam) on Digital Gujarat** — every field highlighted and explained, in order
3. Live two-way voice in **Hindi (primary) + English (Indian)** — both reviewed by the team — plus **Gujarati best-effort** (unreviewed unless a Gujarati speaker checks the glossary + openers). User can interrupt anytime. Official Gujarati terms are copied from the portal, never machine-translated. Pitch angle: a Hindi-speaking migrant worker in Gujarat guided through a Gujarati/English portal.
4. Sourced answers for **Income Certificate, PAN, Driving Licence, ITR** (process level; ITR = guidance, not tax advice)
5. **Scheme suggestions** for Gujarat + central schemes (from myScheme), linked to the documents they need
6. **General mode** on any other form: "I haven't verified this form — general guidance"
7. Privacy: capture only browser window, pauses on password/OTP/PAN/bank fields, nothing stored

**Not in MVP (slides only):** mobile app, REST for mobile, other states, nationwide directory, myScheme API, accessibility-tree precision.

**Fallbacks only if time:** Bhashini voice fallback, live gov.in search.

---

## 2. Data scope (Firecrawl budget: 13k credits, plan ≤1,000)

| Item | Source | Depth |
|---|---|---|
| Income Certificate (Sugam) | digitalgujarat.gov.in service page + **Sugam user manual** + hand-walk | **Field map** (demo) |
| Digital Gujarat portal | registration, login, OTP, payment, helpline pages | Portal profile |
| PAN (new, instant e-PAN, correction) | incometax.gov.in e-filing help, Protean/NSDL, UTIITSL | Process details |
| Driving licence (learner → permanent, Gujarat) | sarathi.parivahan.gov.in (Gujarat) | Process details + portal profile |
| ITR | incometax.gov.in help/FAQ | Process details, no tax advice |
| Schemes | myScheme (Gujarat + central), gssp.gujarat.gov.in | 30–60 schemes |
| Districts/talukas | data.gov.in or lgdirectory.gov.in download | Locations (no credits) |
| Glossary + aliases | from collected pages | Hindi + English reviewed; Gujarati terms copied verbatim from the portal |

**Rules:** official domains only · scrape each page once · extract with your own Gemini key (separate "data" project) · every fact needs a quote from its page or it's `unknown` · test 10 pages first to see real credit cost.

---

## 3. Timeline (clock times)

### 22:45–23:15 · Setup (both)
- [ ] `DECISIONS.md`: Gujarat · Income Certificate (Sugam) · Hindi + English (Gujarati best-effort) · project name (Suraksha or Sahayak)
- [ ] Repo + `PLAYBOOK.md`, `QUICKSTART.md`, this file, `.env.example`
- [ ] Keys: Gemini ×3 projects (demo, backup, data), Sarvam, Firecrawl (Bhashini registration optional)
- [ ] Commit schemas (`shared/schemas/`): form map, service details, scheme, portal profile, source
- [ ] Languages decided: Hindi primary + English; Gujarati best-effort. Ask a Gujarati-speaking friend for a 20-min WhatsApp review of glossary + openers (optional)

### 23:15–01:15 · Data sprint + foundations (parallel)
| P1 — Desktop + Data | P2 — Backend + Voice |
|---|---|
| **Hand-walk Income Certificate (Sugam):** every field, error messages (dummy data), upload rules, screenshots on both OS · download the Sugam manual | Gemini vision returns a field bbox on P1's first screenshots |
| Agent builds + runs pipeline: `discover → fetch → extract → check` in order: Digital Gujarat → PAN → DL → ITR → myScheme | Sarvam: speak Hindi/English → text → spoken reply (quick Gujarati test too) |
| Background agent: Electron overlay + box at fixed coords | FastAPI + WebSocket skeleton + `fake_client.py` |

**01:15 checkpoint:** demo form map v1 · portal profile · ≥10 process records · overlay box on a real field · vision bbox correct · voice round-trip works. Tag `data-v1`.

### 01:15–03:30 · Vertical slice
- P1: capture + crop + frame diff + coordinate mapping + mic/speaker in app · `mock_backend.ts`
- P2: session states `idle → watching → guiding_field` · frame → vision → highlight · voice pipeline (VAD → STT → TTS)
- **03:30 Integration 1 (together):** "guide me" → first field highlighted → explained out loud, both OS

### 03:30–07:00 · Core loop
- P1: highlight follows fields + scroll · pre-written guidance + openers (Hindi + English; Gujarati best-effort) · eval questions
- P2: full field loop · page change · need → link card → consent · sourced answers · interrupt (barge-in) + self-echo guard
- **07:00 GATE (together):** whole Income Certificate form guided, interruptible, 2 languages, both OS. **Fail → both fix only this until it passes.**

### 07:00–13:00 · Robustness + breadth (sleep in shifts)
| Time | P1 | P2 |
|---|---|---|
| 07:00–10:00 | **Sleep** | Error explanations · password/OTP/PAN pause · pre-submit checklist |
| 10:00–13:00 | PAN / DL / ITR Q&A data polish · scheme data checks · UI polish | **Sleep** |

Before sleeping: push + 3 lines in `HANDOFF.md`.

### 13:00–16:00 · Breadth (both)
- P2: scheme suggestions ("Kisaan hoon, koi yojana?") · general mode on any form · language switching mid-conversation
- P1: open a PAN or DL form → general mode + process answers work · start scripts · both-OS checks

### 16:00–17:30 · Hardening (no new features)
- Vision eval, answer eval (unanswerable → "not sure" + link), 10-min rate-limit soak, privacy checks, swap laptops and run full flow

### 17:30–19:00 · Demo prep
- Backup video (main OS + 20-s second-OS clip) · 5 slides · 3 timed rehearsals

### 19:00–20:00 · Freeze
- Fixes only · final rehearsal · charge everything

---

## 4. Demo script (~3 min)
1. Problem: a Hindi-speaking worker in Surat needs an income certificate (આવકનો દાખલો) for their child's scholarship — Gujarati/English portal, agents charge money
2. Speak Hindi: "Mujhe aavak ka praman patra chahiye" → Suraksha asks one question → shows Digital Gujarat link
3. "Guide me" → live highlighting + explanation in Hindi, no hotkey
4. Interrupt with a question → sourced answer → continues; switch to English mid-conversation → follows (optionally a short Gujarati line, labelled best-effort)
5. Wrong format → error explained
6. Pre-submit checklist → "You press Submit"
7. "Which schemes can I get?" → 2–3 schemes, each needing the income certificate
8. Open a PAN form → general mode + sourced PAN process answer
9. Roadmap slide: all categories & states (teammate's data strategy), myScheme API, mobile on same backend

---

## 5. If behind, cut in this order
Live gov.in search → Bhashini fallback → second OS live (recorded clip instead) → ITR (keep PAN + DL) → schemes down to 10.
**Never cut:** the Income Certificate guided flow, interrupt, privacy pause, testing, backup video.
