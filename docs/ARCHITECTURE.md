# Architecture and Phase 0 review

*Written 2026-11-27, Phase 0. Each section says whether it is a fact (with source), a
measurement (with command), an estimate (with its assumptions), or a proposal.*

Contents
1. Summary for the trader
2. Critical review of the brief
3. Failure modes beyond those in the brief
4. Proposed changes (need your yes/no)
5. Assumptions
6. Open questions
7. Sizing feasibility estimate
8. Edge hypotheses (Strategy 1 and the PDH/PDL baseline)
9. Memory budget plan
10. System design
11. How the brief's invariants map to code

---

## 1. Summary for the trader

- The brief is sound. I found **one cost problem**, **one sizing problem** and about a dozen
  design points that are cheaper to settle now than to fix later.
- **Cost problem:** the brief assumes Lightsail's $5 plan has 1 GB RAM / 40 GB SSD. On AWS's
  pricing page today it has **0.5 GB RAM / 20 GB SSD**. The cheapest plan with 1 GB RAM is
  **$7/month** (2 vCPU, 40 GB SSD). My measurements (section 9) say the stack does not
  fit in 0.5 GB with any safety margin. You need to choose (Q1 in section 6).
- **Sizing problem:** with ₹50,000 and 1 lot, a ₹500 risk per trade would take only ~1 in 8
  plausible signals. ₹1,000 takes about half; ₹1,500 about three-quarters but lets one
  loss trip the 3% daily limit. **I propose ₹1,000** (section 7). You decide.
- **Backstop stop:** a fixed 35% backstop on premium would risk ~₹2,700 per NIFTY lot,
  almost 3× the risk budget. I changed the default to a delta-based backstop (1.5× the
  strategy stop, in premium terms). Section 4, P3.
- Flattrade's API documentation site only renders in a browser, so I could not read it
  from here. Everything about Flattrade endpoints is `UNVERIFIED` until Phase 3, when I will
  ask you to save the doc pages for me. This does not block Phases 1–2.

---

## 2. Critical review of the brief

What is strong and should not change:
- Fixed safety invariants, the PAPER-first restart rule, the per-day arming and the
  separate watchdog. These match how small retail algo systems actually fail.
- PDH/PDL as a cost-adjusted baseline. Most "edges" do not beat a plain breakout.
- Recording option-chain snapshots from day one. This is the only way the OI filter can
  ever be tested, and the only source of real option prices for backtests.
- Modeled option leg, labelled as modeled.

Points where the brief is incomplete or where I see a risk:

1. **Lightsail spec is out of date** (fact, aws.amazon.com/lightsail/pricing, read
   2026-11-27). $5 = 0.5 GB / 2 vCPU / 20 GB; $7 = 1 GB / 2 vCPU / 40 GB. Mumbai gets half
   the transfer allowance (still 1 TB on the $7 plan, far more than we need). Static IPs are
   free while attached; an unattached one costs $0.005/hour.
2. **Shared trade slots bias the comparison.** Section 6.2 shares daily trade slots and
   position limits across strategies, with priority deciding who wins. But section 16.3
   compares each strategy with the PDH/PDL baseline "for the same days". If Strategy 1
   (priority 1) takes today's slot, the baseline's signal is `SUPERSEDED`, so the
   baseline's paper record depends on Strategy 1. **Proposal P1:** every strategy also runs
   an *unconstrained* shadow book that takes every one of its own signals (still with its
   own sizing and time guards). Gate statistics come from that book. The shared-slot
   "portfolio" book is what would really be traded. Without this the gate compares two
   strategies on different trade sets.
3. **Invariant 2 also applies to the strategy stop.** The brief describes "target hit →
   cancel SL → confirm → exit". The strategy stop (on the index, section 7.5) is also
   engine-monitored while the backstop SL-LMT rests at the broker, so hitting it needs the
   same cancel-then-exit sequence. That sequence costs a round trip or two (~0.3–1.5 s)
   while price is moving against us. **Proposal P2:** where the broker allows changing an
   order's type and price, exit by *modifying the resting SL-LMT into a marketable LMT*.
   There is only ever one exit order, so there is no race and no gap. Fall back to
   cancel-confirm-place where modify is not possible. (Fyers documents PATCH modify; the
   Flattrade ModifyOrder fields are unverified.)
4. **Sizing uses the strategy stop, but the real worst case is the backstop.** If the
   engine dies, the loss is set by the broker-side backstop, which is deliberately wider.
   **Proposal P3:** the backstop is computed from the strategy stop (delta-based, 1.5×
   by default), and a trade is skipped if the backstop loss would exceed
   `max_backstop_loss_multiple_of_risk` (1.5×) × risk per trade. Logged as `SKIPPED_SIZE`.
5. **The gate is statistically weak at 40 trades.** With a typical intraday option-buying
   win rate of 35–45% and 2R targets, the standard error of expectancy over 40 trades is
   roughly as large as the expectancy itself. Worked example: 40% wins at +2R, 60% losses
   at −1R gives expectancy 0.2R per trade, a per-trade standard deviation of ~1.47R, and a
   standard error over 40 trades of 1.47/√40 ≈ 0.23R. A real 0.2R edge and no edge at all
   are hard to tell apart at that sample size. "Positive expectancy" can pass by luck.
   I will keep the gate exactly as written, and **add** a bootstrap 90% confidence
   interval for expectancy and for "strategy minus baseline" to every gate report, so you
   can see how much luck is in a pass. (Reporting only; it does not change pass/fail.)
6. **40 trades may take a long time.** Strategy 1 needs four conditions together (trend,
   EMA touch with a hammer, RVOL ≥ 2, OI pulse); it may fire well under once a day across
   both indices. Phase 1 backtests will measure signal frequency so you know before
   Phase 2 how many weeks the gate is likely to take.
7. **Arming code sent over the same Telegram chat** protects against a mis-tap, not against
   someone who has your phone or Telegram account. That is acceptable for its purpose, but
   it should be described honestly as an anti-mistake step. Optional stronger version: the
   code is shown on the dashboard (behind its login) and typed into Telegram. Q6.
8. **"Every restart comes up in PAPER" vs. real positions.** After a mid-day crash, any
   real position is rebuilt from the broker (invariant 7) and must keep being managed as
   real (section 5.2.5). So "PAPER after restart" means *no new real-money entries until
   re-armed*, never "manage open real positions as paper". I will implement and test it
   that way.
9. **No automatic execution-broker failover.** The routing config lists a failover
   provider per capability. For execution I will **not** fail over automatically: switching
   broker mid-session would leave open orders and positions at the first broker unmanaged.
   An unhealthy execution broker triggers invariant 14 (no new entries, critical alert).
   Data capabilities do fail over.
10. **Historical trading calendar.** The holiday file is per year and maintained by you.
    For backtests over 2022–2026 I will not ask you to type in old holiday lists: trading
    days in history come from the days on which index futures actually traded in the
    downloaded data. The yearly file is needed only for live and paper trading.
11. **Fyers history depth depends on your plan** (fact, fyers.in/api-docs/v3/getting-started/plans-and-limits,
    updated 2026-10-14): Standard plan intraday history covers the last 1 year, Prime 5+ years.
    Your note says 5-minute futures back to January 2022 worked, which suggests Prime or an
    older rule. 6 months of backtest fits in either. Q4.
12. **Fyers data rate limits are tight on the Standard plan**: 5/s, 50/min, **5,000/day**. A
    30-second chain snapshot for two underlyings is ~1,500 calls a day, which is fine, but
    a backfill run on the same day competes for the same quota. If the per-minute limit is
    exceeded more than 3 times in a day, Fyers blocks the user for the rest of the day.
    The token bucket defaults to 3/s and 45/min, and backfill will refuse to run during
    market hours by default.
13. **Index ticks have no volume and no depth.** The liquidity check (spread, depth) needs
    option quotes, and RVOL needs futures. Both are in the design, but the recorder must
    subscribe to the current future and the ATM±n options, not just the indices. Fyers
    Standard allows 100 WebSocket symbols per user, enough for 2 indices + VIX + 2 futures
    + 2 × 22 option contracts (49 symbols).

---

## 3. Failure modes beyond those in the brief

| # | Failure | Mitigation (phase) |
|---|---|---|
| F1 | Two engines on the **same** host (double-click on Windows, systemd plus a manual start) | Exclusive lock file + DB lease per instance id; second process exits (P2) |
| F2 | OI in the option chain updates only every few minutes at source, so a short window W sees "no change" | Recorder logs when each strike's OI actually changes; Phase 1 reports the real update interval; W must exceed it (P1) |
| F3 | Futures roll changes volume level overnight, so RVOL spikes or drops falsely around the roll | RVOL lookback never spans a roll; `time_of_day` mode uses same-contract days or a volume ratio (P1) |
| F4 | First 1-minute bar of the day: ATR's true range includes the overnight gap, inflating ATR | ATR uses high−low only for the first bar of each session (recorded in DECISIONS.md) (P1) |
| F5 | Square-off fails on expiry day, so an ITM option is exercised: STT on intrinsic value (0.15%) applies, and a large loss is possible | Expiry-day square-off escalates earlier (15:00), with critical alerts and manual instructions (P2) |
| F6 | Price band / circuit limit rejects the protective SL-LMT or the exit limit | Rejection reason is parsed; the limit price is clamped to the band; escalate (P3) |
| F7 | Broker accepts the order but the response is lost (Fyers documents code 201 as "request made, not acknowledged") | Treated as `UNCERTAIN`; tag lookup in the order book before any retry (invariant 5) (P2/P3) |
| F8 | The laptop sleeps or Windows Update reboots it mid-session (PAPER/recorder only) | Runbook power settings; the recorder detects and logs gaps; never used for LIVE (P1/P3) |
| F9 | SQLite file corruption from a power cut on the laptop | WAL with `synchronous=NORMAL`, nightly `.backup`, integrity check at start (P1) |
| F10 | Lightsail instance stopped and started: static IP stays attached, but the public IP check briefly fails | IP check inconclusive means LIVE disabled; re-check every 10 min; alert explains (P3) |
| F11 | Telegram API down at the exact moment of `/flatten` | Dashboard button and CLI are independent routes; watchdog retries and logs (P2) |
| F12 | Clock correct, but the broker's exchange timestamps lag (feed delay) | Tick age measured with the exchange timestamp versus local NTP-synced time, not receive time (P1) |
| F13 | AI provider returns valid JSON that contains an instruction-like "reason" | Verdicts are display-only data; never parsed for commands (P2) |
| F14 | Free-plan AWS account closes at 6 months and takes the static IP with it | Runbook: upgrade before month 5, billing alert; IP re-registration steps (P3) |
| F15 | Weekly expiry moves (holiday) and the date arithmetic is wrong | Expiries only from the instrument master (brief 3.2); test with a shifted-expiry fixture (P1) |

---

## 4. Proposed changes (need your yes/no)

| # | Proposal | Default if you don't answer |
|---|---|---|
| P1 | Each strategy gets an unconstrained shadow book; gate and baseline comparison use it | **Yes** (it adds a book, takes nothing away) |
| P2 | Exit by modifying the resting SL-LMT into a marketable LMT where the broker allows | Yes for Fyers; Flattrade decided in Phase 3 once its docs are read |
| P3 | Delta-based backstop (1.5× strategy stop), skip if backstop loss > 1.5× risk | **Yes** (already the config default; change `contracts.backstop_mode` to undo) |
| P4 | Bootstrap confidence intervals in gate reports (report only) | Yes |
| P5 | Recorder also builds 1-minute candles for the ATM±5 option contracts from ticks | Ask; ~15 MB/day more; makes the "real option P&L" layer much better than 30-second snapshots |
| P6 | Add a 1 GB swap file on the server as a safety net, not as capacity | Yes |
| P7 | Live engine computes indicators incrementally in plain Python/numpy; pandas only in backtests and reports | Yes (saves ~40 MB per process, section 9) |

---

## 5. Assumptions

- A1. You stay on Fyers for market data and Flattrade for execution, as in the brief.
- A2. Python 3.12 (Ubuntu 24.04 LTS system Python; python.org installer on Windows).
  Tested here on 3.13 too.
- A3. Lot sizes in section 7 (NIFTY 65, BANKNIFTY 30) are **assumptions** for the estimate
  only. The platform reads them from the instrument master daily. The 2 Nov 2026 freeze
  quantities (NIFTY 1,800, BANKNIFTY 900; Zerodha bulletin citing NSE FAOP77189) are
  consistent with BANKNIFTY 30 but I could not confirm NIFTY's lot size.
- A4. ATM premiums in section 7 (NIFTY weekly ~₹120, BANKNIFTY monthly ~₹500) are rough
  assumptions. I could not reach NSE for live numbers. Rerun the tool with today's values.
- A5. `instance_id` is 2–4 upper-case letters/digits (e.g. `LAP1`, `AWS1`), so that order
  tags fit every broker's limit (Fyers: 1–30 letters/digits).
- A6. The SEBI rule details (10 orders/s, no market orders, static IP) are taken from the
  Fyers support article (updated 31 Mar 2026) and Flattrade's v2 article (8 Jan 2026). I
  could not read the SEBI or NSE circulars directly (sites returned 403/503).

---

## 6. Open questions (your call)

- **Q1. Server plan:** (a) Lightsail **$7/month, 1 GB** (recommended); (b) $5/month,
  0.5 GB, with swap and the recorder moved to the laptop; this is fragile and I don't
  recommend it for real money; (c) another provider. The brief says "about $5", so this
  is a budget decision.
- **Q2. Risk per trade:** ₹1,000 proposed (section 7). ₹500 skips most trades; ₹1,500 lets
  one loss plus slippage trip the 3% daily breaker.
- **Q3. P5** (record 1-minute option candles): yes / no.
- **Q4. Fyers plan:** Standard or Prime? This decides how far back backfill can go (1 year
  versus 5+ years of intraday data) and the daily request quota (5,000 versus 500,000).
- **Q5. Expiry day for NIFTY:** default is `next_weekly` (trade next week's contract on
  Tuesday). The alternative is `skip` (no NIFTY entries on expiry day).
- **Q6. Arming second factor:** code sent in the same Telegram chat (brief), or shown on
  the dashboard and typed into Telegram (slightly stronger, one more step each morning)?
- **Q7. Consecutive-loss breaker:** does the streak of 3 losses reset each morning, or
  run across days until a win? Default: across days (stricter).

---

## 7. Sizing feasibility estimate (brief section 9.1)

**This is an estimate, not data.** Reproduce it with `python -m app.tools.sizing_estimate`
and rerun it with today's lot sizes and premiums. Phase 1 backtests replace it with the
measured share of signals skipped for size.

Method: risk of 1 lot = (index stop distance × delta 0.5 + slippage) × lot size. A trade is
taken if that is within the risk budget and 1 lot's premium is at most 40% of capital
(₹20,000). Stop distances are plausible values for a 1-minute hammer/breakout-candle stop
plus buffer (NIFTY 1-minute ATR is roughly 8–20 points; BANKNIFTY ~2.2×). Each listed
stop is treated as equally likely, which is itself an assumption.

```
NIFTY (weekly): lot 65, ATM premium ~Rs 120, delta 0.5, 1 lot costs Rs 7,800 (cap Rs 20,000)
| stop (index pts) | 1-lot risk Rs | Rs 500 | Rs 1000 | Rs 1500 |
| 10 | 422   | take | take | take |
| 15 | 585   | skip | take | take |
| 20 | 748   | skip | take | take |
| 25 | 910   | skip | take | take |
| 30 | 1,072 | skip | skip | take |
| 40 | 1,398 | skip | skip | take |
| 50 | 1,722 | skip | skip | skip |
| 60 | 2,048 | skip | skip | skip |
| share taken |  | 12% | 50% | 75% |

BANKNIFTY (monthly): lot 30, ATM premium ~Rs 500, 1 lot costs Rs 15,000
| 25 .. 130 pts | 495 .. 2,070 | 12% | 50% | 75% |   (same pattern)
```

Observations:
- At **₹500** (1% of capital), almost nothing can be taken with 1 lot.
- At **₹1,000** (2%), about half of plausible signals fit: those with tighter stops.
  Because skipping wide-stop signals is a filter in its own right, the backtest will
  report results both for "all signals" (at underlying level) and "signals that fit".
- At **₹1,500** (3%), the daily loss limit is ₹1,500, so one full loss plus slippage
  trips the daily breaker; the max-3-trades rule becomes meaningless.
- If BANKNIFTY's monthly ATM premium is above ~₹667, 1 lot (30 × premium) exceeds the
  40% cap and **every** BANKNIFTY trade is skipped. This is likely in the first half of a
  monthly cycle. Rerun with real premiums.
- **Recommendation: ₹1,000 per trade** (already the config value, marked PROPOSED). It is
  2% of capital, which is high by textbook standards; that is the honest price of trading
  index options with ₹50,000. The risk is never raised silently: a signal that does not fit
  is logged as `SKIPPED_SIZE`.

---

## 8. Edge hypotheses

Written before any backtest, as the research principles require. Parameters are fixed in
config before out-of-sample data is looked at (section 16.4), and the config hash is
recorded in DECISIONS.md before the out-of-sample run.

### 8.1 Strategy 1: EMA200 hammer + RVOL + OI pulse

**Why it might work.** On the 15-minute chart, the 200 EMA marks the slow trend; trading
only in its direction avoids fighting the dominant flow. On the 1-minute chart, a hammer
that tags the 1-minute 200 EMA and closes back above it shows that sellers pushed into a
level many participants watch and were absorbed. High relative volume on that candle (from
futures) says the absorption was real participation, not a quiet drift. Put writing / call
unwinding near ATM over the same window says option sellers are positioning for the same
direction. The idea is that the combination marks the end of a pullback inside a trend,
giving a tight stop (below the hammer) and room for 2R.

**Why it might not work.**
- Each filter cuts the number of trades; four together may leave too few trades to
  measure anything.
- An option *buyer* also pays theta and the spread; a move of 2R on the index may not be
  2R on the premium, especially late in the day or near expiry.
- OI data may update too slowly to mean anything over a 15-minute window (F2).

**What would falsify it.** Any of these, measured out-of-sample after costs:
1. Layer 1 (index only): the share of signals reaching +2R before −1R is not above the
   break-even rate for 2R (33%) by a margin larger than its confidence interval.
2. Layer 2 (modeled option P&L): expectancy after charges is ≤ 0, or is not above the
   PDH/PDL baseline over the same days. That is reported as "no edge over baseline".
3. The sensitivity grid (RVOL 1.5/2.0/2.5, `tol`, `min_range_atr`) shows a result only in
   one isolated cell; that is a sign of curve-fitting, not an edge.
4. In paper (books A/B), adding the OI filter (B vs A) does not improve expectancy; in
   that case the OI filter does not earn its place.

### 8.2 PDH/PDL breakout (baseline)

**Why it might work.** The previous day's high and low are the most widely watched
intraday levels. A 1-minute close beyond one of them can trigger stops and breakout
entries, producing follow-through on days when volatility expands.

**Why it might not work.** Most breakouts on most days fail and reverse (range days).
The stop on the other side of the breakout candle can be wide on a fast candle, and the
option buyer pays theta while waiting.

**Its job.** It is a *benchmark*, not a candidate. It answers "what does a plain breakout
already earn on this data, with these costs?". Even a negative baseline is useful: a
strategy must beat it to show that its extra rules add anything.

**What would falsify it as an edge in its own right.** Expectancy after charges ≤ 0
out-of-sample, or the 2R-before-1R rate not above 33% beyond its confidence interval.
P (plain) versus P+T (with the 15-minute trend filter) shows whether the trend filter alone
adds value.

---

## 9. Memory budget plan (1 GB server)

**Measured here** (Python 3.13, Linux, peak RSS right after import; command in
PROGRESS.md). The independent reviewer measured the same imports on Python 3.12 at
39 / 53 / 65 / 97 MB, so on the server's Python version the numbers are 5–20% lower and
pandas adds ~32 MB. I use the higher 3.13 numbers for budgeting.

| Imports | Peak RSS |
|---|---|
| bare Python | 13 MB |
| pydantic + SQLAlchemy + PyYAML | 40 MB |
| + FastAPI + uvicorn | 60 MB |
| + numpy | 77 MB |
| + pandas | 118 MB |
| fyers-apiv3 REST + data WebSocket only | 44 MB |

**Plan (estimates until the Phase 2 soak test measures them):**

| Component | Process | Libraries | Budget |
|---|---|---|---|
| Ubuntu 24.04 minimal + sshd + chrony + journald | — | — | ~180 MB |
| tailscaled | — | — | ~40 MB |
| Engine + API + Telegram sender (one process) | `engine` | FastAPI, numpy, Fyers/Flattrade clients, **no pandas** | 200 MB |
| Recorder | `recorder` | Fyers client, SQLAlchemy, **no FastAPI, no pandas** | 120 MB |
| Watchdog (owns Telegram polling, runs flatten CLI) | `watchdog` | httpx + broker client only | 60 MB |
| **Total** | | | **~600 MB**, leaving ~400 MB + 1 GB swap |

Rules that keep it there:
- The API runs inside the engine process (saves ~60 MB), but request handlers only read
  in-memory state; database writes go through one writer task; nothing CPU-heavy runs in
  a request.
- pandas is never imported by engine, recorder or watchdog (P7). Backtests and reports run
  on the laptop. A Phase 2 test will fail if pandas appears in those processes.
- Candles and indicators are kept in bounded ring buffers (e.g. 3 × 200 bars per series).
- Ticks are written in batches once a second; raw ticks are pruned after 30 days.
- systemd `MemoryMax=` per unit, so a leak kills one service cleanly instead of the
  whole box, and the watchdog alerts.

**Why not 0.5 GB:** the OS and Tailscale alone take ~220 MB, leaving ~290 MB for three
processes that need ~380 MB. It would run only by leaning on swap, which makes tick
processing latency unpredictable exactly when the market is busy.

---

## 10. System design

```
                    ┌──────────── Telegram ────────────┐
                    │                                  │ commands (polling)
             alerts │                                  ▼
┌───────────────────┴───────────────┐   local auth  ┌──────────────┐
│ engine process                    │◀────channel───│  watchdog    │── heartbeat check
│  asyncio loop                     │──heartbeat──▶ │  (separate)  │── runs flatten CLI
│  ├ data feed (router → Fyers)     │               └──────────────┘    if engine hangs
│  ├ candle builder → indicators    │
│  ├ strategies (priority order)    │      ┌──────────────┐
│  ├ risk / breakers / mode resolve │      │  recorder    │ ticks, candles, chain
│  ├ order state machine            │      │  (separate)  │ snapshots, VIX, daily OHLC
│  ├ execution adapter              │      └──────┬───────┘
│  │   PAPER │ SEMI │ Flattrade │ Fyers           │
│  ├ AI advisory (async, never awaited by orders) │
│  ├ FastAPI + WebSocket (dashboard)              │
│  └ DB writer task ───────────────▶ SQLite (WAL) ◀┘
└───────────────────────────────────┘
```

- **One engine, four modes:** backtest, replay, paper and live differ only in the clock,
  the data feed and the execution adapter (section 14.1). The strategy, risk and state
  machine code is shared.
- **Package layout:** `app/core` (types, tags, scrubbing), `app/config` (schema, loader,
  secrets), `app/db` (models, migrations, strategies-table seeding), `app/adapters`
  (interfaces, router; broker adapters in later phases), `app/strategies` (plugin
  interface, seed rows, registry, plugins), `app/tools` (CLIs: `init_db` and
  `sizing_estimate` now; backfill, flatten later).
- **Config hash:** every signal and trade stores `trade_config_hash`, which covers the
  strategy's params *and* the risk, contract, time-guard, execution and freshness
  settings (`app/config/loader.py`).
- **Frontend:** React + Vite + TypeScript, built to static files. Decision on how built
  files reach the server is in DECISIONS.md (D8).

## 11. How the invariants map to code

| Inv. | Where it will be enforced | Phase |
|---|---|---|
| 1 no short | risk layer pre-send check using broker-confirmed long qty; DB check `qty_open >= 0` (done) | 0/2 |
| 2 one exit order | state machine; P2 modify-to-exit; DB partial unique index, one working exit per position (done, property-tested) | 0/2/3 |
| 3 protect in N s | state machine timer after fill | 2 |
| 4 gap-skipped stop | stop monitor; `execution.gap_chase_*` config | 2 |
| 5 exactly-once | `app/core/tags.py` (done), `orders.client_tag` unique (done), `OrderRequest` rejects non-conforming tags (done), tag lookup before retry (`find_by_tag`, done in interface) | 0/2 |
| 6 fail closed | freshness guards config (done), data-health gate | 1/2 |
| 7 restart rebuild | startup reconciliation | 2/3 |
| 8 breakers persist | `risk_state` table (done), breaker logic | 0/2 |
| 9 AI out of order path | `BaseAIProvider` is advisory-only (done), async fire-and-forget | 2 |
| 10 modes hard to reach | mode resolver, `arming_sessions` (table done) | 2 |
| 11 flatten | UI, watchdog, `app.tools.flatten` | 2/3 |
| 12 rate limits | token buckets per adapter; `rate_limits` config (done, order cap < 10/s validated) | 1/2 |
| 13 time guards | `time_guards` config (done, ordering validated) | 2 |
| 14 no silent downgrade | mode resolver + alerts; router refuses execution failover (done); config rejects `semi_fallback` on options strategies (done) | 0/2 |
| 15 SEMI verified | `semi_tasks` table (done), SEMI broker | 2 |
| 16 one instance | instance id in tags (done), `is_foreign` (done), periodic order-book scan | 0/2 |
| 17 no change while exposed | strategy table service | 2 |
| 18 secrets stay home | `app/core/scrub.py` + repo scan test (done); log/alert/fixture/AI scrubbing | 0/1/2 |
