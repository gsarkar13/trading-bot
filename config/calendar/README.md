# NSE trading calendar

The engine **refuses to trade** if `nse_<year>.yaml` for the current year is missing
(brief section 14.5). Nothing here is scraped from NSE.

## Yearly checklist (do this every December, and when NSE announces a change)
1. Open NSE's official "Trading Holidays" circular for the coming year (nseindia.com →
   Resources → Exchange Communication → Circulars; search "trading holidays").
2. Copy `nse_TEMPLATE.yaml` to `nse_<year>.yaml`.
3. Enter every **Equity / Equity Derivatives** holiday date (not the clearing holidays).
4. Add any special sessions (e.g. Muhurat trading) under `special_sessions`, with times.
5. Set `verified_by` and `source_circular`, commit, and open a PR.

Backtests do not use this file for past years: trading days in history are taken from
the dates on which index futures actually traded in the downloaded data.
