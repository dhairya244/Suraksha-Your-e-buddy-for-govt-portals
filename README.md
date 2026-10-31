# Suraksha

Suraksha is a floating desktop assistant (Windows + macOS) that, with the user's permission, watches their browser, highlights the next field on a government form, explains it out loud in Hindi or English (Gujarati best-effort), answers questions from official sources, and suggests relevant schemes. It never types, clicks, or submits for the user. Demo form: **Gujarat Income Certificate (Sugam)** on [Digital Gujarat](https://www.digitalgujarat.gov.in/).

Read first: [FINAL-PLAN.md](FINAL-PLAN.md) (scope + timeline) · [PLAYBOOK.md](PLAYBOOK.md) (technical details) · [QUICKSTART.md](QUICKSTART.md) · [DECISIONS.md](DECISIONS.md)

## How to run

```sh
brew install uv                                  # once
uv venv --python 3.12 .venv
uv pip install --python .venv -r pyproject.toml --extra dev
cp .env.example .env                             # fill in keys
.venv/bin/python scripts/check_keys.py           # PASS/FAIL per key
.venv/bin/python -m pytest                       # schema tests
```

Backend and desktop start scripts (`run/start-mac.sh`, `run/start-win.ps1`) come later.

## Owners

| Person | Area | Folders |
|---|---|---|
| **P1** | Desktop + Data | `desktop/` `data/` `demo/` `scripts/` `run/` |
| **P2** | Backend + Voice | `backend/` `scripts/eval_*.py` |
| Both (change together) | Contracts | `shared/schemas/` `PLAYBOOK.md` `DECISIONS.md` |
