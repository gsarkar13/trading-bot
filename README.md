# NSE options + stocks algo platform

Research → paper trading → locked live execution, for a ₹50,000 account.
**Current state: Phase 1 (data and research).** Market data only: no orders can be placed.
Your step-by-step guide is `docs/PHASE1_STEPS.md`.

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
python -m app.tools.init_db
```
Creates or upgrades `data/platform.db` (SQLite, WAL mode; ignored by git) and fills the
strategies table. Safe to run again: it never overwrites your edits.

## Tools available now
| Command | What it does |
|---|---|
| `python -m app.tools.login` | daily Fyers login; stores the token encrypted |
| `python -m app.tools.fyers_probe` | records one real response per Fyers call, secrets removed |
| `python -m app.tools.backfill --from A --to B` | downloads history (resumable) |
| `python -m app.services.recorder` | records a trading day (09:00-15:35 IST) |
| `python -m app.tools.daily` | login if needed, then the recorder (what `deploy\windows_daily.bat` runs) |
| `python -m app.tools.backtest run --from A --to B --period in-sample --grid` | backtest + HTML report |
| `python -m app.tools.backtest freeze` | locks parameters before out-of-sample runs |
| `python -m app.tools.replay --date D` | rebuilds a recorded day from ticks |
| `python -m app.tools.sizing_estimate` | Phase 0 sizing estimate |

Adding a strategy: `docs/STRATEGY_GUIDE.md`.
