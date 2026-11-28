# Progress

Re-read this file and DECISIONS.md at the start of every session.

## Current phase: 2 (paper trading) built 2026-11-28; Phase 1's recorded session still needed

### Phase 0: done and signed off (PR #1 merged 2026-11-27)

### Phase 1: done and verified in this environment (2026-11-27)
| Item | Evidence |
|---|---|
| Checks | `ruff check .` all passed; `mypy` strict: no issues in 62 files; `pytest`: see the PR description for the final count (Python 3.13 and 3.12) |
| Fyers symbol master parsing and key mapping (options, futures, indices, equities) | `tests/test_fyers_symbols.py` on **real** master excerpts (`tests/fixtures/fyers/`), incl. the Jan 2027 holiday-shifted expiry |
| Fyers login (URL, redirect parsing with the `code=200` trap, appIdHash vs SDK, 06:30 expiry), encrypted vault, rate limiter, error mapping, chain and tick parsing | `tests/test_fyers_adapter.py` |
| Candle builder (exchange time, grace, duplicates, late ticks, cumulative volume, gaps), 15m/daily resampling, candle store | `tests/test_candles.py` |
| Backfill tool (resume, retry on rate limit, today's chunk never marked done, market-hours guard) | `tests/test_backfill.py` |
| Recorder (ticks, live candles, chain snapshots, EOD REST candles incl. option contracts, prune, backup, summary) | `tests/test_recorder.py`: a 2.5 s "session" with fake broker and stream |
| Indicators vs pandas (EMA, ATR), patterns incl. doji/zero-range/gap, RVOL both modes | `tests/test_indicators.py` |
| Strategy 1 and PDH/PDL plugins, OI pulse, trend filter, trailing; PDH across holidays and long weekends | `tests/test_strategies.py` |
| Black-Scholes (textbook values, put-call parity), expiry calendar, charges (hand-computed) | `tests/test_research_basics.py` |
| Backtest end to end: a planted PDH breakout and a planted Strategy 1 hammer each traded exactly once; IV sensitivity; report files; baseline comparison; out-of-sample lock; grid | `tests/test_backtest.py` |
| Fill rules: stop before target, gap through stop, square-off, long PE, entry gap, target from entry, trailing, expiry-day skip, layer 3 from recorded option candles, backstop, weekly breaker, VIX without look-ahead, expiry-day snapshot grouping, roll days | `tests/test_backtest_fills.py` |
| Recorder survives malformed chain responses and auth errors; option list rebuilt after a restart; DB integrity check | `tests/test_recorder.py` |
| Replay rebuilds candles identical to live, then runs the day through the backtest engine to a ledger | `tests/test_replay.py`, plus a manual run of the command |
| Live processes never import pandas; recorder imports = 66 MB peak RSS | `tests/test_memory_rules.py`; measured command in ARCHITECTURE.md section 9 |

### Trader's remaining runs, now two commands (docs/PHASE1_STEPS.md, 2026-11-28)
- `deploy\windows_phase1.bat` once after market hours: login, probe, ~1 year of history, in-sample backtest + grid, one zip.
- `deploy\windows_daily.bat` on one trading day: record, replay, one zip.

### Not verified (needs the trader's runs)
- Any call against the real Fyers API: login, history, option chain, data socket. Built
  from the docs and the SDK source; the probe (step 2) records real responses as fixtures.
- Real data volume per day and the 12-month disk projection: the recorder's daily summary
  measures it. Estimate: ~25 MB/day of raw ticks (pruned after 30 days) plus ~9 MB/day kept
  permanently, so ~2–3 GB after a year plus 3 backups; well within 40 GB.
- Backtest results: none yet, because no market data exists in this environment.
- Historical lot-size dates and the Thursday→Tuesday expiry cut-over date in
  `config/contracts.yaml` (marked unverified).

### Phase 1 gate (brief section 21)
- [x] all tests pass (here)
- [x] backtest report covering at least 6 months, run by the trader: 2026-11-28, in-sample 3 Dec 2025 – 31 Jul 2026 (163 days). Results: docs/PHASE1_RESULTS.md
- [ ] recorder ran unattended for one full session on the laptop (`windows_daily.bat`)

## Phase 2: paper trading, dashboard, Telegram - built 2026-11-28, PR open for review

What exists now (details: docs/ARCHITECTURE.md, docs/DASHBOARD.md, DECISIONS D52-D59):
- Engine: order/position state machine, paper broker (persisted), SEMI order tasks,
  mode resolution + arming, breakers, time guards, data gate, restart reconciliation,
  flatten, ledger with itemised charges, end-of-day journal, shadow + portfolio books.
- AI advisory (Gemini `gemini-3.5-flash-lite`, Claude `claude-haiku-5-5`): fire-and-forget.
- Dashboard (React, built static, served by the engine) + login; Telegram bot + watchdog;
  standalone flatten; Windows scripts (`windows_trade.bat`, `windows_flatten.bat`,
  `windows_set_password.bat`, `windows_schedule.bat`). Trader steps: docs/PHASE2_STEPS.md

### Phase 2 gate (brief section 21)
- [x] every section 4 invariant has a test that tries to break it (docs/INVARIANT_TESTS.md)
- [x] chaos tests: feed drop mid-trade, duplicate/out-of-order ticks, order timeout,
      partial fill, gap-skipped SL, token expiry mid-session, real `kill -9` with open
      positions then restart, static-IP flip mid-session, foreign-instance order, engine
      hang with /flatten via the watchdog (real hung process killed) - tests/test_chaos.py etc.
- [x] memory soak within 1 GB: `python -m app.tools.soak --days 10` on 2026-11-28 here:
      10 simulated days, ~225k ticks, 190 signals, peak RSS **82 MB**, +0.28 MB/day.
      Not included: the Fyers websocket SDK and uvicorn threads; measure on the PC with
      a real session (Task Manager) before Phase 3.
- [ ] not verified here (needs the trader's PC and a market day): a full live paper
      session end to end with real Fyers ticks, the dashboard on the phone, Telegram.

## Next: Phase 3 (execution broker, server, real-money gate), only after the trader says go

## Things only the trader can do (brief section 23), current list
- [x] Create the private GitHub repo and attach it to the session
- [x] Answer ARCHITECTURE.md section 6, Q1–Q7 (2026-11-27: $7 plan, ₹1,000 risk, defaults for the rest)
- [ ] Tell me if your Fyers account is on the Prime plan (I assume Standard until then)
- [ ] Merge the Phase 0 PR to sign off
- [x] Create the Fyers non-trading API app (2026-11-27)
- [ ] Put its App ID and Secret in `.env` on the laptop (PHASE1_STEPS.md step 1)
- [ ] Phase 1: run `deploy\windows_phase1.bat` (after hours) and `deploy\windows_daily.bat` (one trading day); attach the two zips
- [ ] Check the unverified historical dates in `config/contracts.yaml`
- [ ] Create a Telegram bot with @BotFather (optional, docs/PHASE2_STEPS.md)
- [ ] Get AI keys if wanted (optional)
- [ ] Set the dashboard password; create and verify `config/calendar/nse_2026.yaml` (PHASE2_STEPS.md)
- [ ] AWS: sign up with UPI, create Lightsail instance ($7/month, 1 GB plan), attach static IP, billing alert; upgrade to Paid before month 5 (Phase 3)
- [ ] Create the Flattrade API key with the static IP; register the same IP with Fyers (Phase 3)
- [ ] Save Flattrade Pi doc pages and attach them (Phase 3)
- [ ] Tailscale on server and phone (Phase 3)
- [ ] Create `config/calendar/nse_2027.yaml` from NSE's circular (and 2026 if paper-trading before year end)
- [ ] Daily login and arming (from Phase 2 paper; real money only after gates)
- [ ] Run the first live session yourself with 1 lot
