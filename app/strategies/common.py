"""Building blocks shared by strategy plugins."""

from __future__ import annotations

import datetime as dt

from app.data.bars import Bar
from app.data.resample import bucket_start
from app.indicators.core import ATR, EMA, is_hammer, is_shooting_star
from app.strategies.base import OIPulse

ONE_MIN = dt.timedelta(minutes=1)


class Aggregator:
    """Incremental 1-minute -> N-minute candles, session aligned (09:15 IST).

    `add()` returns the N-minute candle only when its LAST minute has closed, so a
    strategy can only ever see completed higher-timeframe candles (no look-ahead).
    A bucket with a missing minute still completes when a later bucket starts.
    """

    def __init__(self, minutes: int) -> None:
        self.minutes = minutes
        self._start: dt.datetime | None = None
        self._bars: list[Bar] = []

    def _merge(self) -> Bar:
        b = self._bars
        assert self._start is not None
        return Bar(
            self._start,
            b[0].open,
            max(x.high for x in b),
            min(x.low for x in b),
            b[-1].close,
            sum(x.volume for x in b),
        )

    def add(self, bar: Bar) -> Bar | None:
        start = bucket_start(bar.ts, self.minutes)
        done: Bar | None = None
        if self._start is not None and start != self._start:
            done = self._merge()  # previous bucket had a gap at its end
            self._bars = []
        self._start = start
        self._bars.append(bar)
        if bar.ts == start + (self.minutes - 1) * ONE_MIN:
            done = self._merge()
            self._bars, self._start = [], None
        return done


class TrendFilter15:
    """15-minute EMA trend from completed 15-minute candles only (section 7.1)."""

    def __init__(
        self,
        period: int,
        warmup_multiple: float,
        strict: bool,
        n_bars: int,
        min_range_atr: float,
        tol_atr: float,
        tol_points: float = 0.0,
    ) -> None:
        self.agg = Aggregator(15)
        self.ema = EMA(period, warmup_multiple)
        self.atr = ATR(14, warmup_multiple)
        self.strict, self.n_bars = strict, n_bars
        self.min_range_atr, self.tol_atr, self.tol_points = min_range_atr, tol_atr, tol_points
        self.last: Bar | None = None
        self._recent: list[tuple[Bar, float, float]] = []  # (bar, ema, atr)

    def add_1m(self, bar: Bar) -> None:
        b15 = self.agg.add(bar)
        if b15 is None:
            return
        ema = self.ema.update(b15.close)
        atr = self.atr.update(b15)
        self.last = b15
        self._recent = ([*self._recent, (b15, ema, atr)])[-self.n_bars :]

    @property
    def ready(self) -> bool:
        return self.ema.ready and self.last is not None

    def direction(self) -> str | None:
        """'bull', 'bear' or None (exactly on the EMA, or strict-mode pattern missing)."""
        if not self.ready or self.last is None or self.ema.value is None:
            return None
        if self.last.close > self.ema.value:
            d = "bull"
        elif self.last.close < self.ema.value:
            d = "bear"
        else:
            return None
        if self.strict and not self._strict_ok(d):
            return None
        return d

    def _strict_ok(self, d: str) -> bool:
        for b, ema, atr in self._recent:
            tol = self.tol_atr * atr + self.tol_points
            if (
                d == "bull"
                and b.low <= ema + tol
                and is_hammer(b, atr, min_range_atr=self.min_range_atr)
            ):
                return True
            if (
                d == "bear"
                and b.high >= ema - tol
                and is_shooting_star(b, atr, min_range_atr=self.min_range_atr)
            ):
                return True
        return False


def oi_confirms(p: OIPulse | None, direction: str) -> str:
    """'CONFIRM', 'REJECT' or 'UNKNOWN' (section 7.4).

    Bullish: (put OI added or call OI removed) and PCR rising over the window.
    Bearish: (call OI added or put OI removed) and PCR falling.
    """
    if p is None:
        return "UNKNOWN"
    if direction == "bull":
        ok = (p.put_oi_change > 0 or p.call_oi_change < 0) and p.pcr_end > p.pcr_start
    else:
        ok = (p.call_oi_change > 0 or p.put_oi_change < 0) and p.pcr_end < p.pcr_start
    return "CONFIRM" if ok else "REJECT"
