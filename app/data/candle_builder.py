"""Build 1-minute candles from ticks (sections 7.2, 14.4).

Rules:
  * A tick belongs to the minute of its EXCHANGE timestamp, never its receive time.
  * A minute is closed at minute end + `grace_s` (default 2 s) of exchange time, as seen
    from later ticks or the caller's clock (`close_until`).
  * Exact duplicate ticks are dropped; ticks for an already-emitted minute are dropped and
    counted as `late` (they can't change a candle that strategies may have acted on).
  * Volume comes from the cumulative day volume (`vol_traded_today`): a minute's volume is
    the last cumulative value in that minute minus the last value before it. Indices have
    no volume (0). A decreasing cumulative value is treated as 0 and counted.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from app.adapters.base import TickData
from app.data.bars import Bar

ONE_MIN = dt.timedelta(minutes=1)


@dataclass
class _Open:
    start: dt.datetime
    o: float
    h: float
    low: float
    c: float
    first_cum: int | None
    last_cum: int | None


@dataclass
class BuilderStats:
    ticks: int = 0
    duplicates: int = 0
    late: int = 0
    volume_resets: int = 0
    candles: int = 0
    gap_minutes: list[dt.datetime] = field(default_factory=list)


class CandleBuilder:
    def __init__(self, grace_s: float = 2.0) -> None:
        self.grace = dt.timedelta(seconds=grace_s)
        self._open: dict[dt.datetime, _Open] = {}
        self._emitted_until: dt.datetime | None = None  # all minutes < this are final
        self._prev_cum: int | None = None
        self._last_key: tuple[dt.datetime, float, int | None] | None = None
        self.stats = BuilderStats()

    def add(self, tick: TickData) -> list[Bar]:
        """Add a tick; return any candles that are now closed."""
        self.stats.ticks += 1
        ident = (tick.exchange_ts, float(tick.ltp), tick.cum_volume)
        if ident == self._last_key:
            self.stats.duplicates += 1
            return []
        self._last_key = ident
        minute = tick.exchange_ts.replace(second=0, microsecond=0)
        if self._emitted_until is not None and minute < self._emitted_until:
            self.stats.late += 1
            return []
        px = float(tick.ltp)
        b = self._open.get(minute)
        if b is None:
            self._open[minute] = _Open(minute, px, px, px, px, tick.cum_volume, tick.cum_volume)
        else:
            b.h, b.low, b.c = max(b.h, px), min(b.low, px), px
            if tick.cum_volume is not None:
                if b.first_cum is None:
                    b.first_cum = tick.cum_volume
                b.last_cum = max(b.last_cum or 0, tick.cum_volume)
        return self.close_until(tick.exchange_ts)

    def close_until(self, now: dt.datetime) -> list[Bar]:
        """Emit every open minute whose end + grace is <= now (exchange-time clock)."""
        out = []
        for start in sorted(self._open):
            if start + ONE_MIN + self.grace > now:
                break
            out.append(self._emit(self._open.pop(start)))
        return out

    def flush(self) -> list[Bar]:
        """End of session: emit everything still open."""
        return [self._emit(self._open.pop(s)) for s in sorted(self._open)]

    def _emit(self, b: _Open) -> Bar:
        vol = 0
        if b.last_cum is not None:
            base = self._prev_cum if self._prev_cum is not None else b.first_cum
            vol = b.last_cum - (base or 0)
            if vol < 0:
                self.stats.volume_resets += 1
                vol = 0
            self._prev_cum = b.last_cum
        if self._emitted_until is not None and b.start > self._emitted_until:
            m = self._emitted_until
            while m < b.start:
                self.stats.gap_minutes.append(m)
                m += ONE_MIN
        self._emitted_until = b.start + ONE_MIN
        self.stats.candles += 1
        return Bar(ts=b.start, open=b.o, high=b.h, low=b.low, close=b.c, volume=vol)
