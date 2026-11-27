# Progress

Re-read this file and DECISIONS.md at the start of every session.

## Current phase: 0 (review and skeleton), awaiting trader sign-off

### Done and verified (2026-11-27)
| Item | Evidence |
|---|---|
| `.gitignore` committed before anything else | first commit `b4e7ca4` contains only `.gitignore` |
| Repo scaffold, `pyproject.toml`, lint (ruff), type check (mypy strict), tests (pytest + Hypothesis) | `ruff check .` → all passed; `mypy` → no issues in 24 files; `pytest` → **71 passed** (Python 3.13 and 3.12) |
| Config schema with section 22 defaults; per-host override; strategy params validated per plugin; options can't fall back to SEMI; `trade_config_hash` | `tests/test_config.py` |
| Database schema: 24 tables (all 23 from section 19 + `strategy_history`); Alembic migration 0001 | `tests/test_db.py`: migration creates every table; models match migration; WAL on; exact decimals; UTC enforced. PostgreSQL DDL generated offline without errors (no Postgres server here to run it against) |
| DB-level guards for invariants 1 and 2 and LMT-only orders | `tests/test_db_invariants.py`: second working exit refused (incl. Hypothesis over all purpose/status pairs), market order types refused, negative qty refused |
| `OrderRequest` validation (tag format, no index keys, MIS/CNC only, trigger rules) | `tests/test_order_request.py` |
| Strategies table seeded on `init_db`, trader edits preserved | `tests/test_seed.py`; `python -m app.tools.init_db` run twice: 11 rows, then 0 |
| Adapter interfaces (`BaseDataProvider`, `BaseExecutionBroker`, `BaseAIProvider`) and capability router | `tests/test_router.py`: data failover, no execution failover |
| Strategies table (11 seed rows) and plugin interface | `tests/test_strategies_table.py`: placeholders can't activate; plugin segment must match its row; signals carry config hash |
| Order-tag scheme (invariant 5, 16) | `tests/test_tags.py`: Hypothesis round-trip; foreign-instance detection |
| Secret scan (invariant 18 groundwork) | `tests/test_secrets_scan.py`: no token-like strings in repo; `.env`/db not tracked; detector catches `.env`-style lines, `jKey`, bare SHA-256 hex |
| Independent fresh-eyes review | Findings and what was done about each: PR description |
| Dated charges table | `config/charges.yaml`; `tests/test_charges_table.py` |
| Sizing feasibility estimate | `python -m app.tools.sizing_estimate`; results in ARCHITECTURE.md section 7 |
| Memory measurement of planned libraries | ARCHITECTURE.md section 9 (command below) |
| ARCHITECTURE.md, BROKER_NOTES.md, DECISIONS.md | docs/ |

Memory measurement command (Linux):
```bash
python -c "import resource, fastapi, uvicorn, numpy, sqlalchemy, pydantic; print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024, 'MB')"
```

### Not done / not verified
- Flattrade API details: docs site not readable from here (BROKER_NOTES.md).
- Lot sizes and ATM premiums used in the sizing estimate are assumptions.
- `docs/STRATEGY_GUIDE.md` is written in Phase 1, when the PDH/PDL plugin exists to serve as the worked example.
- No broker code, engine, UI, Telegram or deployment scripts yet (Phases 1–3).

## Next: Phase 1 (data and research), only after the trader says go
Fyers data adapter, backfill tool, recorder, instrument mapping with fixtures, indicators /
patterns / RVOL with golden tests, both strategy plugins, replay, backtest with three result
layers + baseline + HTML report, STRATEGY_GUIDE.md.

## Things only the trader can do (brief section 23), current list
- [x] Create the private GitHub repo and attach it to the session
- [x] Answer ARCHITECTURE.md section 6, Q1–Q7 (2026-11-27: $7 plan, ₹1,000 risk, defaults for the rest)
- [ ] Tell me if your Fyers account is on the Prime plan (I assume Standard until then)
- [ ] Merge the Phase 0 PR to sign off
- [ ] Create the Fyers API app (data now; App Type 200 + static IP later); put keys in `.env`
- [ ] Phase 1: run the backfill and backtest commands I give you; run the recorder for one full session
- [ ] Create a Telegram bot with @BotFather (Phase 2)
- [ ] Get AI keys if wanted (Phase 2)
- [ ] AWS: sign up with UPI, create Lightsail instance ($7/month, 1 GB plan), attach static IP, billing alert; upgrade to Paid before month 5 (Phase 3)
- [ ] Create the Flattrade API key with the static IP; register the same IP with Fyers (Phase 3)
- [ ] Save Flattrade Pi doc pages and attach them (Phase 3)
- [ ] Tailscale on server and phone (Phase 3)
- [ ] Create `config/calendar/nse_2027.yaml` from NSE's circular (and 2026 if paper-trading before year end)
- [ ] Daily login and arming (from Phase 2 paper; real money only after gates)
- [ ] Run the first live session yourself with 1 lot
