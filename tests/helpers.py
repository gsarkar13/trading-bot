"""Builders for synthetic bars used across tests."""

from __future__ import annotations

import datetime as dt
import random

from app.core.timeutil import IST
from app.data.bars import Bar


def ist(d: dt.date, h: int, m: int) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=IST).astimezone(dt.UTC)


def bar(ts: dt.datetime, o: float, h: float, low: float, c: float, v: int = 0) -> Bar:
    return Bar(ts=ts, open=o, high=h, low=low, close=c, volume=v)


def session_bars(d: dt.date, start: float, seed: int, n: int = 375, vol: int = 1000) -> list[Bar]:
    """A random-walk 1-minute session from 09:15 for n minutes."""
    rnd = random.Random(seed)
    out, px = [], start
    t0 = ist(d, 9, 15)
    for i in range(n):
        o = px
        c = round(o + rnd.gauss(0, 4), 2)
        h = round(max(o, c) + abs(rnd.gauss(0, 2)), 2)
        lo = round(min(o, c) - abs(rnd.gauss(0, 2)), 2)
        out.append(Bar(t0 + dt.timedelta(minutes=i), o, h, lo, c, int(vol * rnd.uniform(0.5, 1.5))))
        px = c
    return out


def trading_days(start: dt.date, n: int) -> list[dt.date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out
