# NSE options + stocks algo platform

Research → paper trading → locked live execution, for a ₹50,000 account.
**Current state: Phase 0 (skeleton).** No broker connections, no orders, nothing runs yet.

Read first:
- `docs/ARCHITECTURE.md`: review of the brief, design, sizing estimate, open questions
- `docs/PROGRESS.md`: what is done and verified, what is next, your to-do list
- `docs/DECISIONS.md`: every decision with its reason
- `docs/BROKER_NOTES.md`: verified and unverified broker API facts

## Set up a development copy

Linux / macOS:
```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Windows (Command Prompt, Python 3.12 from python.org):
```bat
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

Then copy the examples (both machines):
- `.env.example` → `.env` (secrets; never committed)
- `config/host.example.yaml` → `config/host.yaml` (this machine's instance id and role)

## Checks
```bash
ruff check .
mypy
pytest
```

## Database
```bash
alembic upgrade head
```
Creates `data/platform.db` (SQLite, WAL mode). Ignored by git.

## Tools available now
```bash
python -m app.tools.sizing_estimate --nifty-lot 65 --nifty-premium 120 --bn-lot 30 --bn-premium 500
```
