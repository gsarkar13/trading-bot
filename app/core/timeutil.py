"""IST/UTC helpers. Stored times are UTC; market rules are written in IST (section 14.4)."""

from __future__ import annotations

import datetime as dt

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
SESSION_OPEN = dt.time(9, 15)
SESSION_CLOSE = dt.time(15, 30)


def to_ist(ts: dt.datetime) -> dt.datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime")
    return ts.astimezone(IST)


def from_epoch(seconds: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(seconds, tz=dt.UTC)


def ist_dt(d: dt.date, t: dt.time) -> dt.datetime:
    """An aware datetime for an IST wall-clock time on date d."""
    return dt.datetime.combine(d, t, tzinfo=IST)


def minute_floor(ts: dt.datetime) -> dt.datetime:
    return ts.replace(second=0, microsecond=0)


def session_minute_index(ts: dt.datetime) -> int:
    """Minutes since 09:15 IST of the candle containing ts (0 = the 09:15 candle)."""
    t = to_ist(ts)
    return (t.hour * 60 + t.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)


def in_session(ts: dt.datetime) -> bool:
    t = to_ist(ts).time()
    return SESSION_OPEN <= t < SESSION_CLOSE
