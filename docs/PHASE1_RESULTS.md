# Phase 1 results (in-sample)

*Data run by the trader on 2026-11-28 (bundle `phase1_bundle_20261128_1744.zip`, PC,
Windows 11, Python 3.12.10, code at 55a4cdb). Analysed in the dev environment.*

## In one paragraph

On eight months of real Fyers data (3 Dec 2025 – 31 Jul 2026, 163 trading days),
**no strategy shows an edge after costs that can be told apart from luck**. The PDH/PDL
baseline does slightly better than break-even on the index itself, but once it is traded
through bought options (spread, charges, premium decay) it loses about ₹45 per trade per
lot. Strategy 1 looks positive, but it traded only 22 times in eight months, far too few
to judge. These are in-sample results, so they are the *most* optimistic view we will
get. The out-of-sample run (after freezing parameters) is the real test.

## Data

| | NIFTY | BANKNIFTY |
|---|---|---|
| Trading days | 163 in-sample (244 downloaded, to 27 Nov 2026) | same |
| 1-minute index candles | 61,125 in-sample | 61,125 |
| Futures candle coverage | 100% of index minutes | 100% |
| Option-chain snapshots | none yet (only the recorder creates them) | none |
| Recorded option candles | none yet | none |

Real-data observations, now covered by tests (`tests/test_fyers_recorded.py`):
- One day in the download has only ~70 candles: a special short session (very likely the
  Diwali Muhurat hour). It is outside the in-sample period. From now on, candles outside
  09:15–15:30 are ignored, so such a session can never become the next day's PDH/PDL
  (D48). The next download lists every short or special day by date.
- There are Sunday sessions in the data (3 baseline trades on a Sunday, e.g. Budget day
  1 Feb 2026). These are real full sessions and are kept.
- Fyers now reports volume on the **index** candles too; RVOL still uses futures volume.
- Quotes keep arriving after 15:30 (futures until 15:40, index until 15:59). The 15:30
  filter keeps them out of candles used by strategies.

## Results by book (layer 2 = MODELED option P&L, 1 lot, after charges)

| Book | Trades | Win rate | Index edge (mean R, 90% CI) | ₹ per trade, 1 lot (90% CI) | vs baseline |
|---|---|---|---|---|---|
| S2_P PDH/PDL (baseline) | 201 | 39.8% | **+0.19 R** (+0.03 … +0.36) | **−45** (−144 … +52) | — |
| S2_PT + 15m trend filter | 110 | 38.2% | +0.15 R (−0.07 … +0.36) | −47 (−169 … +75) | no edge over baseline |
| S1_A EMA200 hammer + RVOL | 22 | 40.9% | +0.23 R (−0.32 … +0.77) | +22 (−332 … +404) | not distinguishable |
| S1_B + OI filter | 0 | — | — | — | needs recorded chain data |

How to read it:
- **"Index edge"** is the trade measured on the index in units of its own risk (layer 1).
  With 2R targets, break-even is a 33% win rate. The baseline wins 39.8% of the time, so on
  the index it is slightly profitable, and its confidence interval just stays above zero.
- **"₹ per trade"** is what that becomes when you *buy options* to trade it (layer 2,
  modeled with Black-Scholes and India VIX). Spread, slippage and about ₹42 of charges per
  round trip turn the baseline's small index edge into a loss.
- **IV sensitivity:** the baseline loses at every IV assumption (−31 / −45 / −57 at
  0.8× / 1.0× / 1.2×). Strategy 1 stays positive (+39 / +22 / +6) but its interval spans zero.
- **Trades are very short:** median 5 minutes, longest 65. Stops sit just beyond one
  1-minute candle (about 26 index points on average), so every trade hit its stop or its
  target well before 15:10.
- **Time of day (baseline):** 80 crossings happen in the first 15 minutes, which the time
  guard blocks. Of the ones taken, 09:xx and 10:xx (126 trades) average +0.09 to +0.10 R.

## Sensitivity grid (in-sample only, judged as a whole)

- **Strategy 1:** 26 of 27 cells are positive (median +₹137 per trade, range −₹13 to
  +₹513). Stricter settings (RVOL 2.5, candle range ≥ 1.0 ATR) look best, but they have
  only 10–17 trades. The cells share most of their trades, so this is not 27 independent
  confirmations. Parameters stay at the frozen defaults; no tuning (brief section 1).
- **Baseline buffer** 0.10 / 0.25 / 0.50 ATR: −₹80 / −₹45 / −₹73 per trade. All three lose.

## Findings that matter for you

1. **BANKNIFTY ATM options don't fit a ₹50,000 account.** The real chain shows the
   BANKNIFTY monthly ATM premium at about **₹1,400**, so 1 lot (30) costs about ₹42,000,
   more than twice the 40%-of-capital cap. In the backtest, 68 of the baseline's 103
   BANKNIFTY trades were skipped for size, and the rest only fit late in the monthly cycle.
   *(Question B1 below.)*
2. **The modeled BANKNIFTY cost is too optimistic.** The real bid-ask spread on BANKNIFTY
   near-ATM strikes was 8–32 points (median about 13), against the 3 points assumed. NIFTY's
   spread (about 1 point) matches the assumption. The BANKNIFTY layer-2 numbers above are
   therefore better than reality. I will recalibrate from the recorder's intraday snapshots,
   not from one Friday-close snapshot, and from market data only, never from P&L (D49).
3. **Strategy 1 fires about 2.8 times a month.** The Phase 3 gate asks for 40 paper trades
   over at least 4 weeks; at this rate that takes about **14 months** of paper trading.
   *(Question B2 below.)*
4. **The OI filter (book B) can't be judged yet.** It needs months of recorded
   option-chain snapshots. The recorder starts building them on the first recorded day.

## What these results do *not* show

- Out-of-sample performance. That is the next step: freeze, then run Aug–Nov 2026.
- Real option prices (layer 3). None are recorded yet.
- That Strategy 1 is better than the baseline. Its interval is far too wide.

## Questions for you (answered 2026-11-28: B1 = research only, B2 = keep 40; D50, D51)

- **B1. BANKNIFTY:** (a) keep it in the research books, but stop treating it as tradable at
  this capital (it gets skipped for size anyway); or (b) test a cheaper strike rule for
  BANKNIFTY only, e.g. `delta_target` 0.3, as a new written hypothesis before any backtest.
  Default: (a).
- **B2. Strategy 1's gate sample:** keep 40 paper trades (about 14 months at this rate), or
  set a different minimum now, before paper trading starts? Default: keep 40.
