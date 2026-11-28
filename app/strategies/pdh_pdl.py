"""Strategy 2: PDH/PDL breakout, the baseline benchmark (brief section 8, DECISIONS.md D1).

Kept deliberately simple, so it answers "what does a plain breakout earn on this data?".

  * Levels: previous trading day's high (PDH) and low (PDL) of the index, from completed
    daily candles.
  * Trigger: a 1-minute candle CLOSE that crosses above PDH + buffer (buy CE) or below
    PDL - buffer (buy PE). "Crosses" means the previous 1-minute close was not already
    beyond the level (DECISIONS.md D29), so a gap-open above PDH is not a breakout by
    itself; the price must come back and cross again.
  * At most one trade per direction per underlying per day (counted on trades taken).
  * Optional 15-minute EMA200 trend filter (off by default), for books P vs P+T.
  * Stop: other side of the breakout candle -/+ buffer, or k x ATR14 from the close, or
    whichever is tighter (default).
  * Target: 2R, or a trailing stop.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import ClassVar

from pydantic import BaseModel

from app.config.schema import PdhPdlParams
from app.core.timeutil import to_ist
from app.core.types import Segment
from app.indicators.core import ATR
from app.strategies.base import (
    DataNeeds,
    ExitDecision,
    MarketView,
    OpenPosition,
    Signal,
    StrategyPlugin,
    WarmupRequirement,
)
from app.strategies.common import TrendFilter15
from app.strategies.ema200_hammer import trail_exit
from app.strategies.registry import register


class _State:
    def __init__(self, p: PdhPdlParams) -> None:
        self.atr = ATR(p.atr_period)
        self.trend = TrendFilter15(
            p.trend.ema_period, 3.0, p.trend.strict_hammer, p.trend.strict_n_bars, 0.8, 0.25
        )
        self.prev_close: float | None = None
        self.day: dt.date | None = None
        self.taken: set[str] = set()


@register
class PdhPdlBreakout(StrategyPlugin):
    id: ClassVar[str] = "pdh_pdl_breakout"
    segment: ClassVar[Segment] = Segment.OPTIONS
    params_model: ClassVar[type[BaseModel]] = PdhPdlParams

    def __init__(self, params: BaseModel, config_hash: str) -> None:
        super().__init__(params, config_hash)
        assert isinstance(self.params, PdhPdlParams)
        self.p: PdhPdlParams = self.params
        self._s: dict[str, _State] = {}

    @classmethod
    def data_needs(cls, params: BaseModel) -> DataNeeds:
        return DataNeeds(timeframes=("1m", "1d"))

    @classmethod
    def warmup(cls, params: BaseModel) -> WarmupRequirement:
        assert isinstance(params, PdhPdlParams)
        bars = {"1m": params.atr_period * 3}
        if params.trend_filter_enabled:
            bars["15m"] = params.trend.ema_period * 3
        return WarmupRequirement(bars=bars, prior_daily_candles=1)

    def on_trade_opened(self, signal: Signal) -> None:
        st = self._s.get(signal.underlying)
        if st is not None:
            st.taken.add(signal.direction)

    def on_candle(self, timeframe: str, view: MarketView) -> list[Signal]:
        if timeframe != "1m":
            return []
        p = self.p
        st = self._s.setdefault(view.underlying, _State(p))
        bar = view.bar
        d = to_ist(bar.ts).date()
        if d != st.day:
            st.day, st.taken, st.prev_close = d, set(), None
        atr = st.atr.update(bar)
        st.trend.add_1m(bar)
        prev, st.prev_close = st.prev_close, bar.close
        pd = view.prior_day
        if pd is None or prev is None or not st.atr.ready:
            return []
        if p.trend_filter_enabled and not st.trend.ready:
            return []
        buf = p.buffer_value * atr if p.buffer_mode == "atr" else p.buffer_value
        up, dn = pd.high + buf, pd.low - buf
        if bar.close > up and prev <= up:
            direction, trend_needed = "LONG_CE", "bull"
            candle_stop = bar.low - p.stop_buffer_points
            atr_stop = bar.close - p.stop_atr_k * atr
            stop = {
                "candle": candle_stop,
                "atr": atr_stop,
                "tighter_of_both": max(candle_stop, atr_stop),
            }[p.stop_mode]
        elif bar.close < dn and prev >= dn:
            direction, trend_needed = "LONG_PE", "bear"
            candle_stop = bar.high + p.stop_buffer_points
            atr_stop = bar.close + p.stop_atr_k * atr
            stop = {
                "candle": candle_stop,
                "atr": atr_stop,
                "tighter_of_both": min(candle_stop, atr_stop),
            }[p.stop_mode]
        else:
            return []
        if direction in st.taken:
            return []
        trend = st.trend.direction() if st.trend.ready else None
        if p.trend_filter_enabled and trend != trend_needed:
            return []
        risk = abs(bar.close - stop)
        if risk <= 0:
            return []
        long = direction == "LONG_CE"
        target = bar.close + p.target_rr * risk if long else bar.close - p.target_rr * risk
        checklist: dict[str, object] = {
            "pdh": pd.high,
            "pdl": pd.low,
            "buffer": round(buf, 2),
            "level": round(up if long else dn, 2),
            "prev_close": prev,
            "close": bar.close,
            "atr14": round(atr, 2),
            "trend_15m": trend,
            "trend_filter": p.trend_filter_enabled,
            "stop_mode": p.stop_mode,
            "distance_to_level": round(bar.close - (up if long else dn), 2),
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
