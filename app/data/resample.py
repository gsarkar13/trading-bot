"""Build 15-minute and daily bars from 1-minute bars, aligned to the NSE session.

15-minute buckets start at 09:15 IST: 09:15-09:29, 09:30-09:44, ..., 15:15-15:29.
A bucket is *complete* only when its last minute has closed; callers must use completed
buckets only (no look-ahead, section 7.1).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator
from itertools import groupby

from app.core.timeutil import IST, session_minute_index, to_ist
from app.data.bars import Bar


def _merge(bars: list[Bar], ts: dt.datetime) -> Bar:
    return Bar(
        ts=ts,
        open=bars[0].open,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
        close=bars[-1].close,
        volume=sum(b.volume for b in bars),
        oi=bars[-1].oi,
    )


def bucket_start(ts: dt.datetime, minutes: int) -> dt.datetime:
    idx = session_minute_index(ts)
    if idx < 0:
        raise ValueError(f"{ts} is before the session open")
    t = to_ist(ts)
    open_ = t.replace(hour=9, minute=15, second=0, microsecond=0)
    return (open_ + dt.timedelta(minutes=(idx // minutes) * minutes)).astimezone(dt.UTC)


def resample(bars: Iterable[Bar], minutes: int) -> Iterator[tuple[Bar, bool]]:
    """Yield (bar, complete). `complete` is False only for a trailing partial bucket."""
    bars = list(bars)
    groups = [(k, list(g)) for k, g in groupby(bars, key=lambda b: bucket_start(b.ts, minutes))]
    for i, (start, group) in enumerate(groups):
        last_minute = start + dt.timedelta(minutes=minutes - 1)
        complete = group[-1].ts >= last_minute or i < len(groups) - 1
        yield _merge(group, start), complete


def daily(bars: Iterable[Bar]) -> list[Bar]:
    """One bar per IST trading date, ts = 09:15 IST of that date."""
    out = []
    for d, group in groupby(bars, key=lambda b: to_ist(b.ts).date()):
        g = list(group)
        out.append(_merge(g, dt.datetime.combine(d, dt.time(9, 15), IST).astimezone(dt.UTC)))
    return out
