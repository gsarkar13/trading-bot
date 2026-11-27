# Adding a strategy

Adding a strategy takes **one plugin file, one config block and one table row**. The
engine, backtester, recorder and risk code don't change (brief section 6.4). The worked
example below is the PDH/PDL baseline, `app/strategies/pdh_pdl.py`.

## What a plugin is, and what it is not

A plugin reads completed candles and returns **signals**. A signal is a direction, a
reference price, a stop and a target, plus a checklist of every input that produced it.

A plugin never talks to a broker, never sizes a position and never places an order. Those
jobs belong to the engine, risk and execution layers. The same plugin code therefore runs
unchanged in backtest, replay, paper and live (section 14.1).

## Step 1: parameters (`app/config/schema.py`)

Add a Pydantic model holding every tunable number, with its default. The PDH/PDL one:

```python
class PdhPdlParams(_Strict):
    underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY")
    buffer_mode: Literal["points", "atr"] = "atr"
    buffer_value: NonNegFloat = 0.25
    atr_period: PosInt = 14
    trend_filter_enabled: bool = False
    stop_mode: Literal["candle", "atr", "tighter_of_both"] = "tighter_of_both"
    stop_buffer_points: NonNegFloat = 5
    stop_atr_k: NonNegFloat = 1.5
    target_mode: Literal["rr", "trail"] = "rr"
    target_rr: Annotated[float, Field(gt=0)] = 2.0
```

Then register the model in `STRATEGY_PARAMS` in the same file:

```python
STRATEGY_PARAMS = {..., "pdh_pdl_breakout": PdhPdlParams}
```

Two rules are enforced when the config loads:
- A mistyped parameter name in `config/*.yaml` is an error, never silently ignored.
- Every signal and trade stores the `trade_config_hash` of the parameters that produced it.

## Step 2: the plugin file (`app/strategies/<name>.py`)

```python
@register
class PdhPdlBreakout(StrategyPlugin):
    id = "pdh_pdl_breakout"  # must match the table row (step 4)
    segment = Segment.OPTIONS  # must match the table row's segment
    params_model = PdhPdlParams

    @classmethod
    def data_needs(cls, params):
        return DataNeeds(timeframes=("1m", "1d"))

    @classmethod
    def warmup(cls, params):
        return WarmupRequirement(bars={"1m": 42}, prior_daily_candles=1)

    def on_candle(self, timeframe, view):  # called once per completed 1-minute candle
        ...  # return [] or [Signal(...)]

    def on_trade_opened(self, signal):  # optional: the engine took one of your signals
        ...  # PDH/PDL counts one trade per direction per day

    def exit_logic(
        self, position, view
    ):  # optional trailing; stop/target hits are the engine's job
        return trail_exit(position, view, self.p.target_mode)
```

What `view` (`MarketView`, in `app/strategies/base.py`) gives you on each candle:

| Field | Meaning |
|---|---|
| `bar` | the completed 1-minute index candle |
| `now` | its close time (UTC) |
| `futures_bar` | the same minute's front-month futures candle (index volume is always 0) |
| `prior_day` | the previous **trading** day's daily candle (holidays and long weekends are skipped for you) |
| `oi_pulse` | option-chain change over the window, or `None` when unknown or stale |
| `volume_series_reset` | a futures roll just happened: restart volume lookbacks |
| `new_session` | first candle of the day |

Rules for writing `on_candle`:
1. **Build your own indicators** from `app/indicators/core.py` (`EMA`, `ATR`, `RVOLSma`,
   `RVOLTimeOfDay`, `is_hammer`, `is_shooting_star`) and `app/strategies/common.py`
   (`Aggregator` for 15-minute candles, `TrendFilter15`). Keep separate state per
   underlying, because one plugin instance sees NIFTY and BANKNIFTY interleaved.
2. **No look-ahead.** Use only completed candles. `Aggregator.add()` returns a 15-minute
   candle only once its last minute has closed.
3. **Fail closed.** Return nothing until every indicator reports `.ready`, and nothing when
   an input you need is `None`.
4. **Put every input in the checklist.** It is stored with the signal, and the dashboard
   shows it (Phase 2).
5. Don't apply time guards, position limits, sizing or cooldowns. The engine and
   backtester apply them identically for every strategy.

Finally, add the module to `PLUGIN_MODULES` in `app/strategies/plugins.py`.

## Step 3: config block (`config/default.yaml`)

```yaml
strategies:
  - id: pdh_pdl_breakout
    active: false
    requested_mode: PAPER
    allow_semi: false
    params: {}          # empty = every default from PdhPdlParams
```

## Step 4: table row (`app/strategies/seed.py`)

Every strategy, implemented or not, has a row with a priority, a stage and notes. A
placeholder becomes `implemented` automatically once its plugin is registered; nobody
edits that flag by hand.

## Step 5: tests and a written hypothesis

- Write the edge hypothesis before any backtest: why it should make money, and what
  result would prove it doesn't (ARCHITECTURE.md section 8 has the two current ones).
- Add tests built from hand-made candles, as in `tests/test_strategies.py`. Cover the
  signal firing, not firing, warm-up and every rule that limits it.
- Add a book in `app/tools/backtest.py::make_books`. Every report then compares it with
  the PDH/PDL baseline automatically.
