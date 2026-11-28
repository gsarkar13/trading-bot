# Dashboard

The dashboard is served by the engine process at **http://127.0.0.1:8600** (this machine
only). It is a single page, dark, and works on a phone.

## First use

1. Set a password (stored only as a scrypt hash in `.env`):
   `python -m app.tools.set_password`
2. Start the engine (`deploy\windows_engine.bat`, or `python -m app.services.engine_service`).
3. Open http://127.0.0.1:8600 and log in.

Without a password the dashboard still opens on 127.0.0.1, but real-money actions
(arming, raising the mode ceiling to SEMI/LIVE, resetting breakers) are refused.

## Phone access to the server

Use Tailscale: put the server's Tailscale IP in `dashboard.extra_bind_hosts`. The engine
refuses to open any extra address until a password is set. Never bind to `0.0.0.0`.

## What is where

| Tab | Contents |
|---|---|
| Trade | top bar (engine RUNNING/PAUSED, mode ceiling, arming, host badge, prices, order-rate meter, P&L, breakers), **EMERGENCY FLATTEN ALL** (type `FLATTEN`), open positions with SL buttons, SEMI order tasks, chart (1m/15m, 200 EMA, PDH/PDL), latest checklist per strategy with the AI verdict |
| Strategies | strategies table (on/off, requested and effective mode with the reason, gate, warm-up, priority arrows), books vs the PDH/PDL baseline, AI what-if |
| Ledger | every closed trade: gross, itemised charges (hover), net, rule-compliance score, overrides, `SELF_REPORTED` |
| Events | event console, filter by category |
| Review | end-of-day journal and AI review |
| Settings | which keys are set (last 4 characters only), AI/data/execution slots, routing, risk, gate thresholds - read-only (D56) |

Rules the dashboard enforces:
* Every button that changes something sends `X-Algo: 1`; other websites can't (CSRF).
* Real-money actions need a password login within the last 5 minutes.
* Moving a stop is allowed only toward the price; each move is a logged rule override
  and lowers that trade's compliance score by 25.

## Building the UI (developers only)

The server never needs Node.js: the built files in `app/api/static/` are committed (D8).
After changing `frontend/src/`:

```
cd frontend
npm install
npm run build
```
