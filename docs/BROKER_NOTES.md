# Broker notes

Facts about external APIs, each with its source and the date it was read. Anything not
read from an official page is marked **UNVERIFIED** and must be confirmed before code
depends on it. Update entries in place; don't duplicate.

Status legend: ✅ read on an official page · ⚠️ UNVERIFIED (lead only) · ❌ not found

---

## Regulatory (SEBI retail algo framework, effective 1 Apr 2026)

| Fact | Status | Source (read 2026-11-27) |
|---|---|---|
| Orders accepted only from a registered App ID mapped to a whitelisted static IP | ✅ | support.fyers.in — "What are the new SEBI rules for retail algo trading from April 01 2026" (updated 31 Mar 2026) |
| Static-IP validation applies to *order* requests | ✅ | same |
| Daily 2FA; "continuous refresh-token sessions will not be supported" | ✅ | same |
| Max 10 orders/second; beyond may be rejected | ✅ | same; Fyers plans-and-limits page |
| Market orders are converted to MPP (market price protection) orders at Fyers | ✅ | same. **We still never send market orders** (brief section 24) |
| Flattrade: only LMT and SL-LMT allowed; no cover/bracket orders | ✅ | flattrade.in/kosh/…api-version-2-launch (8 Jan 2026) |
| SEBI / NSE circular text itself | ❌ | sebi.gov.in returned 403, nseindia.com returned 503 |

---

## Fyers API v3 (data broker; second execution broker)

Docs root: https://fyers.in/api-docs/v3/ (pages say "Last updated on October 14, 2026").

### Auth
| Fact | Status | Source |
|---|---|---|
| Step 1: `GET /api/v3/generate-authcode` with `client_id` (app id, e.g. `XXXX-100`), `redirect_uri` (must match the app's), `response_type=code`, `state` (random, verify on return). Example host `https://api-t1.fyers.in`. | ✅ | …/authentication/individual-app/generate-auth-code/ |
| Redirect carries `auth_code` and `state`; response attributes also list `s`, `code`, `message` | ✅ | same |
| Literal redirect containing `code=200` next to `auth_code=` | ⚠️ | Your field note (brief 3.3). Not shown on the doc page. The parser takes **only** `auth_code` and tests the trap |
| Step 2: `POST /api/v3/validate-authcode`, JSON `{grant_type: "authorization_code", appIdHash, code}` | ✅ | …/validate-auth-code/ |
| `appIdHash` = SHA-256 of `app_id:app_secret` (the example uses the colon form; the attribute table says "api_id + app_secret") | ✅ / ⚠️ | same. Contract test in Phase 1 against your recorded exchange |
| Response includes `access_token` (and `refresh_token` in the example, though refresh sessions are no longer supported) | ✅ | same |
| Header for every call: `Authorization: <app_id>:<access_token>` | ✅ | …/api/authentication/ |
| Token expires daily at **06:30 IST**; log in after 06:30 | ✅ | same |
| Auth error codes: -8 expired, -15 invalid, -16 auth failure, -17 invalid/expired | ✅ | troubleshooting guide |
| Login endpoint: 10 requests per 2 minutes, else IP may be temporarily blocked | ✅ | troubleshooting guide |

### Apps and static IP
| Fact | Status | Source |
|---|---|---|
| Only **App Type 200** (App ID ending in `-200`) can place orders | ✅ | troubleshooting guide |
| Static IP is set in the API dashboard (https://fyers.in/web/api-dashboard/user-apps); can be changed **once in 7 days**; one IP normally maps to one user | ✅ | troubleshooting guide |
| Do **not** use the "Automate" webhook route | brief | brief section 3.3 / 24 |
| Whether order book / positions / funds calls work from a non-registered IP | ❌ | Not stated. Only order requests are said to be IP-checked. Test on the laptop in Phase 2 (read-only call) |

### Rate limits (plans page)
| | Standard | Prime |
|---|---|---|
| Data APIs | 5/s, 50/min, 5,000/day | 10/s, 500/min, 500,000/day |
| Transaction + user-info APIs | 10/s, 200/min, 100,000/day | 10/s, 500/min, 500,000/day |
| Data WebSocket symbols (per user, all connections) | 100 | 5,000 |
| Penalty | per-minute limit exceeded > 3 times in a day → blocked for the rest of the day | |
| Over-limit response | HTTP 429 with `Retry-After` / `X-Retry-After-Ms` | |
Source: https://fyers.in/api-docs/v3/getting-started/plans-and-limits/ ✅ (which endpoint is in which category is inferred).

### Historical data ✅ (…/market-data/historical-data/)
- SDK: `fyers.history(data={...})`. Params: `symbol`, `resolution`, `date_format` (0 epoch / 1 yyyy-mm-dd), `range_from`, `range_to`, `cont_flag` (1 = continuous futures/options), `oi_flag` (1 = add OI).
- Resolutions: `5S,10S,15S,30S,45S`, `1,2,3,5,10,15,20,30,60,120,240`, `D`/`1D`, `1W`, `1M`.
- Per request: minute resolutions **≤ 100 days**; daily/weekly/monthly **≤ 366 days**; seconds 30 trading days.
- Depth: Standard: daily 5 years, **intraday last 1 year**. Prime: daily 25+ years, intraday 5+ years.
- Candle = `[epoch, open, high, low, close, volume(, oi)]`. To avoid a partial candle, set `range_to` at least one bar before now.
- Expired option contracts return "Invalid symbol" (your field note, brief 3.3) ⚠️; the plans page also limits expired F&O intraday to 10 requests/day on Standard.

### Option chain ✅ (…/market-data/option-chain/)
- Params: `symbol`, `strikecount` (≤ 50 each side), `timestamp` (expiry), `greeks="1"` for greeks.
- Response: `optionsChain[]` (first element is the underlying, `strike_price = -1`), `expiryData[]` (`date` DD-MM-YYYY, `expiry` epoch), `indiavixData`, `callOi`, `putOi`.
- Per strike: `ask, bid, ltp, oi, oich, prev_oi, volume, option_type, strike_price, symbol, fyToken`; with greeks: `delta, gamma, theta, vega, iv`.
- India VIX symbol: `NSE:INDIAVIX-INDEX`.

### Orders ✅ (…/order-placement/sync/single-order/)
- Fields: `symbol, qty, type, side, productType, limitPrice, stopPrice, validity, disclosedQty, offlineOrder, stopLoss, takeProfit, orderTag, isSliceOrder`.
- `type`: 1 Limit, 2 Market (MPP), 3 Stop/SL-M (MPP), **4 Stop-limit (SL-L)**. We use only 1 and 4.
- `side`: 1 buy, −1 sell. `productType`: `INTRADAY`, `CNC` (equity), `MARGIN` (derivatives carry), `MTF`. `validity`: `DAY` / `IOC`.
- Prices must be multiples of tick size; F&O qty must be a multiple of lot size.
- **`orderTag`: 1–30 letters or digits, no spaces/special characters, not the client ID, not "Untagged".** Our tags are 14–16 alphanumerics (`app/core/tags.py`).
- Response `id` = order number. **Code 201 = request made but not acknowledged → check the order book before retrying** (invariant 5).
- Place + modify + cancel together share the 10/s limit.
- Modify = PATCH, cancel = DELETE (…/order-management/sync/); body fields ⚠️ not read yet.
- Order book: get all / by id / **by order tag** (GET). Status codes (…/appendix/order-status/): 1 cancelled, 2 traded, 4 transit, 5 rejected, 6 pending.
- Positions: GET; exit-position endpoints exist (DELETE). **We never use "exit position" endpoints**; every exit is our own tagged LMT order.
- Error codes: -50 invalid params, -51 invalid order id, -99 order rejected, -300 invalid symbol, -429 rate limit.

### Symbology ✅ (…/appendix/symbology-format/)
- Equity: `NSE:SBIN-EQ`.
- Futures: `NSE:NIFTY20OCTFUT` = `{Ex}:{Underlying}{YY}{MMM}FUT`.
- Monthly options: `NSE:NIFTY20OCT11000CE` = `{Ex}:{Underlying}{YY}{MMM}{Strike}{CE|PE}`.
- Weekly options: `NSE:NIFTY2010811000CE` = `{Ex}:{Underlying}{YY}{M}{dd}{Strike}{CE|PE}`, with `M` = 1–9, `O` (Oct), `N` (Nov), `D` (Dec).
- Index: `NSE:NIFTY50-INDEX` seen in the data-socket docs ✅; `NSE:NIFTYBANK-INDEX` ⚠️ (your note; not on the page).
- **We never build these strings.** Instruments are mapped from the symbol master by key; this table exists only to read and test the master.

### Symbol master ✅ (…/symbol-master/symbol-master-file/)
- CSV: `https://public.fyers.in/sym_details/NSE_FO.csv` (derivatives), `NSE_CM.csv` (cash).
- Columns, in order: Fytoken, Symbol details, Exchange instrument type, Minimum lot size, Tick size, ISIN, Trading session, Last update date, Expiry date, Symbol ticker, Exchange, Segment, Scrip code, Underlying symbol, Underlying scrip code, Strike price, Option type (CE/PE/XX), Underlying FyToken, reserved ×3.
- Freeze qty (`qtyFreeze`) is only in the JSON master.

### Data WebSocket ✅ (…/websockets/data-socket/overview/)
- Python: `from fyers_apiv3.FyersWebsocket import data_ws`; `FyersDataSocket(access_token="appid:token", litemode=False, reconnect=True, on_message=…)`; `subscribe(symbols=[…], data_type="SymbolUpdate" | "DepthUpdate")`.
- Message types: `sf` (equity/option), `if` (index), `dp` (depth).
- Tick fields: `symbol, ltp, open/high/low_price, prev_close_price, vol_traded_today, last_traded_time, bid_price, ask_price, bid_size, ask_size, last_traded_qty, tot_buy_qty, tot_sell_qty, avg_trade_price`. Sample payloads also show `exch_feed_time` ⚠️ (not in the attribute table). We will use the exchange timestamp, never receive time, for candle building.
- WSS URL not shown (the SDK handles it).

### SDK
- PyPI `fyers-apiv3` ✅ (sdks-and-libraries page); import `fyers_apiv3`. Version 3.3.0 downloaded from PyPI on 2026-11-27. Data-socket limits need ≥ 3.1.0.
- Measured import cost: ~44 MB RSS (REST + data socket).

---

## Flattrade Pi API v2 (primary execution broker)

**Docs site https://pi.flattrade.in/docs renders only in a browser** (it returned an
84-character page shell to my fetch tool). Nothing below about endpoints is verified.
**Action for Phase 3:** I'll ask you to save the doc pages (Ctrl+S, or Print → PDF) and
attach them.

### From the official v2 article ✅ (flattrade.in/kosh/flattrade-api-implementation-of-retail-algo-standards-api-version-2-launch, 8 Jan 2026)
- API keys can be generated only with a **redirect URL and a static IP**.
- Orders can be placed / modified / cancelled **only from the static IP**. (Read-only calls not mentioned.)
- Primary static IP mandatory, secondary optional; **either may be changed once per calendar week**; IPs must be unique across clients.
- "No change in our existing OAuth flow to generate a token every day."
- A v2 session disconnects any v1 session and vice versa. **Use v2 only.**
- One API key for non-registered algos (< 10 orders/second).
- Only limit and SL-limit orders; no cover or bracket orders.
- The WebSocket subscription process changed in v2 (details in the Pi docs).

### Leads from a third-party client library ⚠️ (all UNVERIFIED)
- REST base `https://piconnect.flattrade.in/PiConnectAPI`; body `jData=<json>&jKey=<token>` (Noren style).
- Login `https://auth.flattrade.in/?app_key=<key>`; redirect gives `request_code`.
- Token `https://authapi.flattrade.in/trade/apitoken` with `api_key`, `request_code`, `api_secret = SHA-256(api_key + request_code + api_secret)`.
- WebSocket `wss://piconnect.flattrade.in/PiConnectWSAPI/`.
- Scrip master under `https://flattrade.s3.ap-south-1.amazonaws.com/scripmaster`.
- Endpoints: PlaceOrder, ModifyOrder, CancelOrder, OrderBook, SingleOrdHist, TradeBook, PositionBook, Holdings, Limits, GetQuotes, SearchScrip, GetOptionChain, TPSeries.
- Client tag field (`remarks`?) and its maximum length: unknown. Our tags are 14–16 alphanumerics.

### Unknown, blocks SEMI auto-reconciliation on the laptop
- Whether OrderBook / PositionBook work from a non-registered IP. Until known, SEMI on the
  laptop relies on your confirmation and marks trades `SELF_REPORTED` (brief 5.4).

### Charges ✅ (flattrade.in/charges, read 2026-11-27)
- Brokerage **NIL** in every segment.
- DP charge: "₹20 per debit transaction + GST" (the page says per *debit transaction*, not
  per scrip; which one applies to a multi-scrip sell day is ⚠️).
- STT, exchange, SEBI, stamp, GST rates match the consolidated table in `config/charges.yaml`.
- RMS square-off / call-and-trade: ₹50 + GST per order. Applies if Flattrade's RMS squares
  off a position we failed to close. Another reason for the 15:10 square-off.

---

## Extension points (not built; brief section 14.2)

| Broker | What an adapter would need |
|---|---|
| Angel One (SmartAPI) | API key + TOTP login; static IP registration for orders; its own scrip master (JSON); order tag field `ordertag` (length to check) |
| 5paisa | OAuth + daily login; scrip master CSV; static IP for orders |
| Upstox (API v2/v3) | OAuth daily token; instrument master JSON/CSV; static IP for orders; `tag` field on orders |
| Dhan (DhanHQ) | Access token; static IP for orders; `correlationId` as client tag; security-ID master CSV |
| TrueData | Market data only (no execution); paid; WebSocket + historical REST; would implement `BaseDataProvider` only |

Each needs: one adapter class implementing `BaseDataProvider` and/or `BaseExecutionBroker`
(`app/adapters/base.py`), one `routing` config entry, symbol-master fixture tests, and a
section in this file. All of the above is from general knowledge, ⚠️ unverified.

---

## Hosting facts (not a broker, kept here for the runbook)

| Fact | Status | Source (read 2026-11-27) |
|---|---|---|
| Lightsail Linux IPv4: $5 = 0.5 GB / 2 vCPU / 20 GB / 1 TB; **$7 = 1 GB / 2 vCPU / 40 GB / 2 TB**; $12 = 2 GB / 60 GB | ✅ | aws.amazon.com/lightsail/pricing |
| Mumbai gets half the listed transfer allowance | ✅ | same |
| Instances created on or before 29 Jun 2023 have 1 vCPU | ✅ | same |
| Static IP free while attached; $0.005/h when unattached (after 1 h); 5 per account | ✅ | aws.amazon.com/lightsail/faq |
| AWS Free plan: up to $200 credits; account closes after 6 months or when credits run out | ✅ | aws.amazon.com/free |
| Whether Lightsail is covered by Free-plan credits | ❌ | not confirmed |
| Tailscale Personal: free, non-commercial use, unlimited devices, up to 6 users | ✅ | tailscale.com/pricing |
