"""Incremental indicators. One `update()` per completed bar; O(1) memory per indicator.

The same classes run in backtest, replay, paper and live (section 14.1), so there is no
pandas-vs-live divergence. Golden tests compare them with pandas.

Warm-up (invariant 6): an indicator is `ready` only after `warmup_bars` updates. For EMAs
that is `warmup_multiple x period` (default 3x, section 7.1).
"""

from __future__ import annotations

import datetime as dt
from collections import deque

from app.core.timeutil import session_minute_index, to_ist
from app.data.bars import Bar


class EMA:
    """Matches pandas `Series.ewm(span=period, adjust=False).mean()`; seeded with the first
    value."""

    def __init__(self, period: int, warmup_multiple: float = 3.0) -> None:
        if period <= 0:
            raise ValueError("period must be positive")
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.warmup_bars = max(1, round(period * warmup_multiple))
        self.value: float | None = None
        self.count = 0

    def update(self, x: float) -> float:
        self.value = x if self.value is None else self.alpha * x + (1 - self.alpha) * self.value
        self.count += 1
        return self.value

    @property
    def ready(self) -> bool:
        return self.count >= self.warmup_bars


class ATR:
    """Wilder ATR: RMA of true range with alpha = 1/period, seeded with the first TR.

    DECISIONS.md D15: the first bar of each session uses high - low only, so the overnight
    gap does not inflate the opening ATR. Matches pandas
    `tr.ewm(alpha=1/period, adjust=False).mean()` on that TR series.
    """

    def __init__(self, period: int = 14, warmup_multiple: float = 3.0) -> None:
        self.period = period
        self.alpha = 1.0 / period
        self.warmup_bars = max(1, round(period * warmup_multiple))
        self.value: float | None = None
        self.count = 0
        self._prev_close: float | None = None
        self._prev_date: dt.date | None = None

    @staticmethod
    def true_range(bar: Bar, prev_close: float | None) -> float:
        if prev_close is None:
            return bar.high - bar.low
        return max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))

    def update(self, bar: Bar) -> float:
        d = to_ist(bar.ts).date()
        prev = self._prev_close if d == self._prev_date else None
        tr = self.true_range(bar, prev)
        self.value = tr if self.value is None else self.alpha * tr + (1 - self.alpha) * self.value
        self._prev_close, self._prev_date = bar.close, d
        self.count += 1
        return self.value

    @property
    def ready(self) -> bool:
        return self.count >= self.warmup_bars


class RVOLSma:
    """RVOL = this bar's volume / mean volume of the previous `lookback` completed bars."""

    def __init__(self, lookback: int = 20) -> None:
        self.lookback = lookback
        self._hist: deque[int] = deque(maxlen=lookback)

    def value_for(self, volume: int) -> float | None:
        """RVOL of a new bar, computed BEFORE it is added to the history."""
        if len(self._hist) < self.lookback:
            return None
        mean = sum(self._hist) / self.lookback
        return None if mean <= 0 else volume / mean

    def add(self, volume: int) -> None:
        self._hist.append(volume)

    def reset(self) -> None:
        """Call on a futures roll so the lookback never spans two contracts (F3)."""
        self._hist.clear()

    @property
    def ready(self) -> bool:
        return len(self._hist) >= self.lookback


class RVOLTimeOfDay:
    """RVOL = volume / mean volume of the same session minute over the last N sessions.

    Corrects the U-shaped intraday volume profile (section 7.3).
    """

    def __init__(self, sessions: int = 20) -> None:
        self.sessions = sessions
        self._by_minute: dict[int, deque[int]] = {}
        self._dates: deque[dt.date] = deque(maxlen=sessions)

    def value_for(self, bar: Bar) -> float | None:
        hist = self._by_minute.get(session_minute_index(bar.ts))
        if hist is None or len(hist) < self.sessions:
            return None
        mean = sum(hist) / len(hist)
        return None if mean <= 0 else bar.volume / mean

    def add(self, bar: Bar) -> None:
        idx = session_minute_index(bar.ts)
        self._by_minute.setdefault(idx, deque(maxlen=self.sessions)).append(bar.volume)
        d = to_ist(bar.ts).date()
        if not self._dates or self._dates[-1] != d:
            self._dates.append(d)

    def reset(self) -> None:
        self._by_minute.clear()
        self._dates.clear()

    @property
    def ready(self) -> bool:
        """True once N sessions have been seen. A minute slot with less history still
        returns None from value_for(), which blocks the signal (fail closed)."""
        return len(self._dates) >= self.sessions


# ---------------------------------------------------------------- candle patterns
def _parts(bar: Bar, tick: float) -> tuple[float, float, float, float]:
    rng = bar.high - bar.low
    body = max(abs(bar.close - bar.open), tick)  # doji guard (section 7.2)
    upper = bar.high - max(bar.open, bar.close)
    lower = min(bar.open, bar.close) - bar.low
    return rng, body, upper, lower


def is_hammer(
    bar: Bar,
    atr: float,
    *,
    min_range_atr: float,
    wick_to_body_min: float = 2.0,
    opposite_wick_max: float = 0.25,
    tick: float = 0.05,
) -> bool:
    """Bullish hammer (section 7.2)."""
    rng, body, upper, lower = _parts(bar, tick)
    if rng <= 0 or rng < min_range_atr * atr:
        return False
    return (
        lower >= wick_to_body_min * body
        and upper <= opposite_wick_max * rng
        and bar.close >= bar.low + rng * 2.0 / 3.0
    )


def is_shooting_star(
    bar: Bar,
    atr: float,
    *,
    min_range_atr: float,
    wick_to_body_min: float = 2.0,
    opposite_wick_max: float = 0.25,
    tick: float = 0.05,
) -> bool:
    """Bearish rejection; also covers the 'inverted hammer' shape (one name, section 7.2)."""
    rng, body, upper, lower = _parts(bar, tick)
    if rng <= 0 or rng < min_range_atr * atr:
        return False
    return (
        upper >= wick_to_body_min * body
        and lower <= opposite_wick_max * rng
        and bar.close <= bar.high - rng * 2.0 / 3.0
    )
