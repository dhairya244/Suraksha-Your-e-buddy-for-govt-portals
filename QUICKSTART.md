# Suraksha — 1-Page Quick Start (read this, agents read PLAYBOOK.md)

**Humans read this page. Agents read the full `PLAYBOOK.md`.** Paste this into every agent first:
> "Read PLAYBOOK.md fully. You work for P1/P2 on <area>. Follow §0A, the build steps in §10–12, and the contracts in §7–9 exactly. Only edit files in <your folders>. Verify SDK calls against the docs in §4.4."

---

## First 60 minutes (together)
1. Pick **one form**, one state, **2 languages** (Hindi + one from: Bengali, Tamil, Telugu, Gujarati, Kannada, Malayalam, Marathi, Punjabi, Odia)
2. Get keys: Gemini ×2 projects, Sarvam, Bhashini (register now — may be slow), Firecrawl
3. Make repo, commit `PLAYBOOK.md`, start agents on the schemas (§7) and message format (§8.1). **Don't change these later without telling each other.**

## Who does what
| P1 — Desktop + Data (TypeScript) | P2 — Backend + Voice (Python) |
|---|---|
| `desktop/` `data/` `demo/` `scripts/` `run/` | `backend/` |

## Your checklist
| Hour | P1 | P2 |
|---|---|---|
| 1–4 | Overlay box on a real form field (both OS) · **walk the form by hand: note every field, trigger errors, screenshot every page** | Gemini finds fields on P1's screenshots · Sarvam: speak → text → spoken reply |
| 4–8 | Screen capture → send frames · draw highlights from backend · mic + speaker · form map JSON | WebSocket server · frame → vision → highlight · voice pipeline with interrupt |
| **8** | **Integrate together:** "guide me" → field highlighted + explained | ← same |
| 8–12 | Highlight follows scroll · glossary · pre-written guidance + opener audio | Full field-by-field loop · link card → consent · sourced answers · stop-when-user-speaks |
| **12** | **GATE together:** whole form works in 2 languages, interruptible, both OS. If not → fix only this | ← same |
| 12–16 | Pause on password/OTP · schemes list (15–20) · *sleep 13–16* | Error explanations · pre-submit checklist · Bhashini fallback |
| 16–20 | UI polish · start scripts | Scheme suggestions · REST endpoints · language switching · *sleep 16–19* |
| 20–22 | Test everything on both laptops | Run evals · fix bugs |
| 22–25 | Record backup video | Slides | 
| 25–27 | Rehearse ×3 · freeze code · rest | ← same |

## 6 rules
1. **Sync 10 min every 2 hours** — show a working demo, not a status update
2. **Never change a message/schema format silently**
3. **Blocked >15 min?** Use your fake (`mock_backend.ts` / `fake_client.py`) and keep going
4. **Push small commits to `main` often**; before sleeping, write 3 lines in `HANDOFF.md`
5. **Headset mic** for testing and demo (otherwise Suraksha hears itself)
6. **Dummy data only** — never real Aadhaar/phone/names

## Data: don't map every page
Only map the pages on your **demo path** (the screens you'll actually show on stage, usually 2–4). Skip help pages, other services, and anything after submit except the "what happens next" info.

## If behind, cut in this order
Live gov search → stuck check-ins → 2nd OS live (show a recorded clip) → REST endpoints → schemes down to 5. **Never cut:** testing, backup video, privacy rules.
