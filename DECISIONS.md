# Decisions

One line per settled decision, with the time. Agents read this file. Scope/timing: FINAL-PLAN.md overrides PLAYBOOK.md.

| Time | Decision |
|---|---|
| Sat 31 Oct 22:50 | **Project name:** Suraksha |
| Sat 31 Oct 22:50 | **State:** Gujarat |
| Sat 31 Oct 22:50 | **Demo form:** Income Certificate (Sugam) on https://www.digitalgujarat.gov.in/ |
| Sat 31 Oct 22:50 | **Languages:** Hindi (primary), English (Indian); Gujarati best-effort (official Gujarati terms copied verbatim from the portal, never machine-translated) |
| Sat 31 Oct 22:50 | **Firecrawl budget:** ≤1,000 of 13k credits; scrape each page once; test 10 pages first to measure real cost |
| Sat 31 Oct 22:50 | **Data scope:** Income Certificate, PAN, Driving Licence (Gujarat), ITR (guidance only, not tax advice), schemes (myScheme Gujarat + central) |
| Sat 31 Oct 22:50 | **Python:** 3.12 via uv (`.venv`) |
| Sat 31 Oct 22:50 | **Schema ids:** `FormMap.process_id` = `Service.service_id` (e.g. `gj_income_certificate`) |

## Still open (fill in)
| Decision | Value |
|---|---|
| Main stage OS | |
| Gemini models + free-tier RPM/RPD | |
| Sarvam voices per language | (check_keys uses bulbul:v3 `priya` for hi-IN; bulbul:v4-flash also available) |
