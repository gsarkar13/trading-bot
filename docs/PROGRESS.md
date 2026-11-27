# Progress

Re-read this file and DECISIONS.md at the start of every session.

## Current phase: 1 (data and research), built; waiting for the trader's data runs

### Phase 0: done and signed off (PR #1 merged 2026-11-27)

### Phase 1: done and verified in this environment (2026-11-27)
| Item | Evidence |
|---|---|
| Checks | `ruff check .` all passed; `mypy` strict: no issues in 62 files; `pytest`: 158 passed on Python 3.13 and 3.12 |
| Fyers symbol master parsing and key mapping (options, futures, indices, equities) | `tests/test_fyers_symbols.py` on **real** master excerpts (`tests/fixtures/fyers/`), incl. the Jan 2027 holiday-shifted expiry |
| Fyers login (URL, redirect parsing with the `code=200` trap, appIdHash vs SDK, 06:30 expiry), encrypted vault, rate limiter, error mapping, chain and tick parsing | `tests/test_fyers_adapter.py` |
| Candle builder (exchange time, grace, duplicates, late ticks, cumulative volume, gaps), 15m/daily resampling, candle store | `tests/test_candles.py` |
| Backfill tool (resume, retry on rate limit, today's chunk never marked done, market-hours guard) | `tests/test_backfill.py` |
| Recorder (ticks, live candles, chain snapshots, EOD REST candles incl. option contracts, prune, backup, summary) | `tests/test_recorder.py`: a 2.5 s "session" with fake broker and stream |
| Indicators vs pandas (EMA, ATR), patterns incl. doji/zero-range/gap, RVOL both modes | `tests/test_indicators.py` |
| Strategy 1 and PDH/PDL plugins, OI pulse, trend filter, trailing; PDH across holidays and long weekends | `tests/test_strategies.py` |
| Black-Scholes (textbook values, put-call parity), expiry calendar, charges (hand-computed) | `tests/test_research_basics.py` |
| Backtest end to end: planted breakout traded exactly once, three layers, IV sensitivity, report files, baseline comparison, out-of-sample lock, grid, breakers | `tests/test_backtest.py` |
| Replay rebuilds candles identical to live | `tests/test_replay.py` |
| Live processes never import pandas; recorder imports = 66 MB peak RSS | `tests/test_memory_rules.py`; measured command in ARCHITECTURE.md section 9 |

### Not verified (needs the trader's runs, docs/PHASE1_STEPS.md)
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
- [ ] backtest report covering at least 6 months, run by the trader (PHASE1_STEPS.md step 4)
- [ ] recorder ran unattended for one full session on the laptop (step 5)

## Next: Phase 2 (paper trading, dashboard, Telegram), only after the trader says go

## Things only the trader can do (brief section 23), current list
- [x] Create the private GitHub repo and attach it to the session
- [x] Answer ARCHITECTURE.md section 6, Q1–Q7 (2026-11-27: $7 plan, ₹1,000 risk, defaults for the rest)
- [ ] Tell me if your Fyers account is on the Prime plan (I assume Standard until then)
- [ ] Merge the Phase 0 PR to sign off
- [x] Create the Fyers non-trading API app (2026-11-27)
- [ ] Put its App ID and Secret in `.env` on the laptop (PHASE1_STEPS.md step 1)
- [ ] Phase 1: run steps 2–5 of docs/PHASE1_STEPS.md and attach the files
- [ ] Check the unverified historical dates in `config/contracts.yaml`
- [ ] Create a Telegram bot with @BotFather (Phase 2)
- [ ] Get AI keys if wanted (Phase 2)
- [ ] AWS: sign up with UPI, create Lightsail instance ($7/month, 1 GB plan), attach static IP, billing alert; upgrade to Paid before month 5 (Phase 3)
- [ ] Create the Flattrade API key with the static IP; register the same IP with Fyers (Phase 3)
- [ ] Save Flattrade Pi doc pages and attach them (Phase 3)
- [ ] Tailscale on server and phone (Phase 3)
- [ ] Create `config/calendar/nse_2027.yaml` from NSE's circular (and 2026 if paper-trading before year end)
- [ ] Daily login and arming (from Phase 2 paper; real money only after gates)
- [ ] Run the first live session yourself with 1 lot
