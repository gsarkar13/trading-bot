# Decisions

One entry per decision: what, why, date. Update entries in place when a decision changes,
and note what it replaced. "Pending trader" means it is a proposal awaiting a yes/no.

| # | Date | Decision | Reason | Status |
|---|---|---|---|---|
| D1 | 2026-11-27 | **PDH/PDL baseline uses the brief's section 8 defaults**: 1-min close beyond PDH + buffer / PDL − buffer (buffer 0.25 × ATR14 by default); max 1 trade per direction per underlying per day; stop = tighter of (other side of breakout candle ± 5 pts) and 1.5 × ATR14; target 2R; 15-min EMA200 trend filter available, off by default | Trader confirmed there is no separate written version | Decided (trader) |
| D2 | 2026-11-27 | Python ≥ 3.12; Pydantic v2 config; SQLAlchemy 2 + Alembic; SQLite WAL default | Ubuntu 24.04 LTS ships 3.12; brief section 14.8 | Decided |
| D3 | 2026-11-27 | Money and prices stored as exact decimals (TEXT on SQLite, NUMERIC on Postgres); floats are rejected at the DB layer | Rounding errors in P&L and tick-size checks are silent bugs | Decided |
| D4 | 2026-11-27 | All timestamps stored as UTC; naive datetimes rejected at the DB layer | Brief section 14.4; a naive IST time stored as UTC is a 5.5-hour bug | Decided |
| D5 | 2026-11-27 | Order tag = `<instance_id 2–4><YYMMDD><seq 6>`, alphanumeric, ≤ 16 chars | Fyers orderTag allows 1–30 letters/digits; Flattrade's limit is unknown, so stay short; instance id embedded for invariant 16 | Decided; recheck against Flattrade docs in Phase 3 |
| D6 | 2026-11-27 | `code_status` is derived from the plugin registry, not stored as an editable flag | "Only implemented strategies can be switched on" can't be bypassed by editing a row | Decided |
| D7 | 2026-11-27 | Execution capability never fails over automatically to another broker | Switching broker mid-session orphans open orders/positions; invariant 14 applies instead | Decided |
| D8 | 2026-11-27 | Frontend: built in the dev environment and the **built static files are committed** (under `app/web/static/`, Phase 2). CI rebuilds and fails if the committed build is stale | The server pulls with a read-only deploy key, which cannot download release assets from a private repo; committing keeps `update.sh` to a `git pull`. No Node on the server | Decided |
| D9 | 2026-11-27 | API runs inside the engine process; recorder and watchdog are separate processes | Saves ~60 MB on a 1 GB server (measured import cost); watchdog must survive an engine hang | Decided; verified by the Phase 2 soak test |
| D10 | 2026-11-27 | pandas is never imported by engine, recorder or watchdog | pandas adds ~40 MB per process (measured) | Proposed P7 (default yes) |
| D11 | 2026-11-27 | Backstop SL default is delta-based: entry − 1.5 × (strategy stop distance × delta); skip if its loss would exceed 1.5 × risk per trade | A fixed 35% backstop risks ~₹2,700/lot on NIFTY ATM, ~2.7× the proposed risk budget | Proposed P3 (default yes); fixed-% still available |
| D12 | 2026-11-27 | Default `risk_per_trade_inr` = 1,000 | Phase 0 estimate: ₹500 takes ~12% of plausible signals, ₹1,000 ~50%, ₹1,500 ~75% but one loss trips the 3% daily limit | **Pending trader (Q2)** |
| D13 | 2026-11-27 | Server plan | $5 Lightsail plan is now 0.5 GB / 20 GB; 1 GB costs $7/month | **Pending trader (Q1)** |
| D14 | 2026-11-27 | Historical trading days are taken from the dates index futures traded in downloaded data; yearly holiday files are needed only for live/paper | Avoids asking the trader to type years of past holidays; brief 14.5 still enforced for live | Decided |
| D15 | 2026-11-27 | ATR on the first 1-minute bar of a session uses high − low only (no overnight gap) | Otherwise the opening ATR is inflated by the gap and filters (`min_range_atr`, `tol`) misfire for ~14 bars | Decided; applies in Phase 1 |
| D16 | 2026-11-27 | AI model IDs left as `UNSET-choose-in-phase-2` | Brief: pick current low-cost IDs from provider docs at build time (Phase 2) | Decided |
| D17 | 2026-11-27 | Phase 0 work is on branch `claude/gallant-allen-6ug7lw` (the branch this cloud session is allowed to push), not a `phase-0` branch | Session push permissions; later phases follow the same rule unless the trader creates phase branches | Decided |
