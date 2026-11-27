"""Strategy 1: EMA200 hammer + RVOL + OI pulse (brief section 7). Options, NIFTY/BANKNIFTY.

Evaluated on each completed 1-minute candle:
  1. 15-minute trend: last COMPLETED 15m close vs 15m EMA200 -> CE only / PE only.
  2. EMA interaction on 1m: bullish low <= EMA200(1m) + tol and close > EMA; bearish mirror.
  3. Candle: hammer (bullish) / shooting star (bearish), range >= min_range_atr x ATR14.
  4. RVOL >= threshold, from futures (default) or option volume.
  5. OI pulse: CONFIRM / REJECT / UNKNOWN. The signal carries the result; the engine or
     backtester decides by book (A ignores it, B requires CONFIRM) and by
     `oi_unknown_policy`.
Every input is recorded in the signal's checklist. Nothing fires until every indicator
has finished warming up (invariant 6).
"""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel

from app.config.schema import Ema200HammerParams
from app.core.types import Segment
from app.data.bars import Bar
from app.indicators.core import ATR, EMA, RVOLSma, RVOLTimeOfDay, is_hammer, is_shooting_star
from app.strategies.base import (
    DataNeeds,
    ExitDecision,
    MarketView,
    OpenPosition,
    Signal,
    StrategyPlugin,
    WarmupRequirement,
)
from app.strategies.common import TrendFilter15, oi_confirms
from app.strategies.registry import register


class _State:
    def __init__(self, p: Ema200HammerParams) -> None:
        self.trend = TrendFilter15(
            p.trend.ema_period,
            p.warmup_multiple,
            p.trend.strict_hammer,
            p.trend.strict_n_bars,
            p.min_range_atr,
            p.tol_value if p.tol_mode == "atr" else 0.0,
            p.tol_value if p.tol_mode == "points" else 0.0,
        )
        self.ema = EMA(p.ema_period_1m, p.warmup_multiple)
        self.atr = ATR(p.atr_period)
        self.rvol: RVOLSma | RVOLTimeOfDay = (
            RVOLSma(p.rvol_lookback) if p.rvol_mode == "sma20" else RVOLTimeOfDay(p.rvol_lookback)
        )


@register
class Ema200HammerRvolOi(StrategyPlugin):
    id: ClassVar[str] = "ema200_hammer_rvol_oi"
    segment: ClassVar[Segment] = Segment.OPTIONS
    params_model: ClassVar[type[BaseModel]] = Ema200HammerParams

    def __init__(self, params: BaseModel, config_hash: str) -> None:
        super().__init__(params, config_hash)
        assert isinstance(self.params, Ema200HammerParams)
        self.p: Ema200HammerParams = self.params
        self._s: dict[str, _State] = {}

    @classmethod
    def data_needs(cls, params: BaseModel) -> DataNeeds:
        assert isinstance(params, Ema200HammerParams)
        return DataNeeds(
            timeframes=("1m", "15m"),
            needs_futures_volume=params.rvol_source == "futures",
            needs_option_chain=params.oi_filter_enabled,
        )

    @classmethod
    def warmup(cls, params: BaseModel) -> WarmupRequirement:
        assert isinstance(params, Ema200HammerParams)
        m = params.warmup_multiple
        return WarmupRequirement(
            bars={"15m": int(params.trend.ema_period * m), "1m": int(params.ema_period_1m * m)}
        )

    def _volume(self, view: MarketView) -> int | None:
        if self.p.rvol_source == "futures":
            return view.futures_bar.volume if view.futures_bar is not None else None
        return view.option_volume

    def on_candle(self, timeframe: str, view: MarketView) -> list[Signal]:
        if timeframe != "1m":
            return []
        p = self.p
        st = self._s.setdefault(view.underlying, _State(p))
        bar = view.bar
        if view.volume_series_reset:
            st.rvol.reset()  # never span a futures roll (F3)
        vol = self._volume(view)
        vbar = None if vol is None else Bar(bar.ts, bar.open, bar.high, bar.low, bar.close, vol)
        # RVOL compares this bar's volume with PREVIOUS bars, so read it before adding.
        rvol = None
        if vol is not None and vbar is not None:
            rvol = (
                st.rvol.value_for(vol) if isinstance(st.rvol, RVOLSma) else st.rvol.value_for(vbar)
            )
        # Trend = last 15m candle completed at or before this minute's close (a 15m candle
        # ending this minute is complete now, so using it is not look-ahead). The 1m EMA and
        # ATR include this completed candle, as a chart shows them at its close.
        st.trend.add_1m(bar)
        trend = st.trend.direction()
        ema = st.ema.update(bar.close)
        atr = st.atr.update(bar)
        if vol is not None and vbar is not None:
            if isinstance(st.rvol, RVOLSma):
                st.rvol.add(vol)
            else:
                st.rvol.add(vbar)
        ready = st.ema.ready and st.atr.ready and st.trend.ready and st.rvol.ready
        if not ready or trend is None or rvol is None:
            return []
        tol = p.tol_value * atr if p.tol_mode == "atr" else p.tol_value
        kw = {
            "min_range_atr": p.min_range_atr,
            "wick_to_body_min": p.lower_wick_to_body_min,
            "opposite_wick_max": p.opposite_wick_to_range_max,
        }
        if trend == "bull":
            touch = bar.low <= ema + tol and bar.close > ema
            pattern = is_hammer(bar, atr, **kw)
            stop = bar.low - p.stop_buffer_points
            direction = "LONG_CE"
        else:
            touch = bar.high >= ema - tol and bar.close < ema
            pattern = is_shooting_star(bar, atr, **kw)
            stop = bar.high + p.stop_buffer_points
            direction = "LONG_PE"
        if not (touch and pattern and rvol >= p.rvol_threshold):
            return []
        oi = oi_confirms(view.oi_pulse, trend) if p.oi_filter_enabled else "DISABLED"
        risk = abs(bar.close - stop)
        target = (
            bar.close + p.target_rr * risk if trend == "bull" else bar.close - p.target_rr * risk
        )
        checklist: dict[str, object] = {
            "trend_15m": trend,
            "ema15": st.trend.ema.value,
            "close15": st.trend.last.close if st.trend.last else None,
            "ema1m": round(ema, 2),
            "atr14": round(atr, 2),
            "tol": round(tol, 2),
            "ema_touch": touch,
            "pattern": pattern,
            "rvol": round(rvol, 3),
            "rvol_threshold": p.rvol_threshold,
            "rvol_source": p.rvol_source,
            "oi": oi,
            "oi_pulse": None
            if view.oi_pulse is None
            else {
                "put_chg": view.oi_pulse.put_oi_change,
                "call_chg": view.oi_pulse.call_oi_change,
                "pcr_start": round(view.oi_pulse.pcr_start, 4),
                "pcr_end": round(view.oi_pulse.pcr_end, 4),
            },
        }
        return [
            Signal(
                strategy_id=self.id,
                underlying=view.underlying,
                direction=direction,
                at=view.now,
                reference_price=Decimal(str(bar.close)),
                stop_underlying=Decimal(str(round(stop, 2))),
                target_underlying=Decimal(str(round(target, 2))) if p.target_mode == "rr" else None,
                checklist=checklist,
                config_hash=self.config_hash,
            )
        ]

    def exit_logic(self, position: OpenPosition, view: MarketView) -> ExitDecision:
        return trail_exit(position, view, self.p.target_mode)


def trail_exit(position: OpenPosition, view: MarketView, mode: str) -> ExitDecision:
    """Trailing rule used when target_mode = "trail" (DECISIONS.md D30): once price is 1R
    in favour, the stop moves to entry; after that it trails 1R behind the best close."""
    if mode != "trail":
        return ExitDecision(action="HOLD")
    r = position.initial_risk
    long = position.direction in ("LONG_CE", "LONG")
    c = view.bar.close
    gain = (c - position.entry_underlying) if long else (position.entry_underlying - c)
    if gain < r:
        return ExitDecision(action="HOLD")
    new = max(position.entry_underlying, c - r) if long else min(position.entry_underlying, c + r)
    better = new > position.stop if long else new < position.stop
    if better:
        return ExitDecision(
            action="TRAIL",
            new_stop_underlying=Decimal(str(round(new, 2))),
            reason="trail 1R behind close",
        )
    return ExitDecision(action="HOLD")
