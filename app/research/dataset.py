"""Assemble backtest inputs from the local database (filled by backfill and the recorder).

Per underlying:
  * 1-minute index candles: REST history ("fyers_hist") preferred, else live-built ("fyers_ws")
  * 1-minute continuous front-month futures candles (volume for RVOL)
  * daily index candles for PDH/PDL: stored daily candles, else built from 1-minute
  * India VIX (1-minute, else daily) for modeled IV
  * option-chain snapshots -> OI pulse points (only days the recorder ran)
  * recorded option 1-minute candles and stored masters (layer 3, real option P&L)
Trading days = days that have index candles (DECISIONS.md D14).
"""

from __future__ import annotations

import bisect
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, text

from app.core.timeutil import to_ist
from app.data.bars import Bar
from app.data.resample import daily
from app.data.store import load_bars
from app.strategies.oi import ChainPoint

SOURCES = ("fyers_hist", "fyers_ws")


@dataclass
class UnderlyingData:
    underlying: str
    bars: list[Bar]
    futures: dict[dt.datetime, Bar]
    daily: dict[dt.date, Bar]
    trading_days: list[dt.date]
    chain_points: list[ChainPoint] = field(default_factory=list)

    def prior_day(self, d: dt.date) -> Bar | None:
        """Previous TRADING day's daily candle (skips weekends, holidays, long weekends)."""
        i = bisect.bisect_left(self.trading_days, d) - 1
        return self.daily.get(self.trading_days[i]) if i >= 0 else None


@dataclass
class Dataset:
    per_underlying: dict[str, UnderlyingData]
    vix_1m: list[Bar]
    vix_daily: dict[dt.date, Bar]
    option_bars: dict[str, dict[dt.datetime, Bar]]  # canonical key -> ts -> bar
    recorded_expiries: dict[tuple[str, dt.date], list[dt.date]]  # (u, day) -> expiries
    _vix_ts: list[dt.datetime] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._vix_ts = [b.ts for b in self.vix_1m]

    def vix_at(self, ts: dt.datetime) -> float | None:
        """Close of the latest VIX candle that had CLOSED by ts (its start + 1 minute <= ts);
        else the previous day's daily close. Never a value from the future."""
        i = bisect.bisect_right(self._vix_ts, ts - dt.timedelta(minutes=1)) - 1
        if i >= 0 and to_ist(self.vix_1m[i].ts).date() == to_ist(ts).date():
            return self.vix_1m[i].close
        d = to_ist(ts).date()
        prior = [k for k in self.vix_daily if k < d]
        return self.vix_daily[max(prior)].close if prior else None

    @property
    def trading_days(self) -> list[dt.date]:
        return sorted({d for u in self.per_underlying.values() for d in u.trading_days})


def _daily_map(
    engine: Engine, key: str, bars_1m: list[Bar], a: dt.date | None, b: dt.date | None
) -> dict[dt.date, Bar]:
    built = {to_ist(x.ts).date(): x for x in daily(bars_1m)}
    stored = {to_ist(x.ts).date(): x for x in load_bars(engine, key, "1d", SOURCES, a, b)}
    return {**built, **stored}


def load_chain_points(
    engine: Engine, underlying: str, a: dt.date, b: dt.date, on_expiry_day: str = "next_weekly"
) -> list[ChainPoint]:
    """One point per snapshot of the contract that is actually traded that day.

    On an expiry day the recorder snaps two expiries a moment apart (D24). Mixing them
    would make OI 'change' by the difference between two different contracts, so only
    the traded expiry is kept: the nearest, or the next one on its expiry day when
    `on_expiry_day` is "next_weekly".
    """
    q = text(
        "SELECT snapshot_id, taken_at, expiry, spot, strike, option_type, oi "
        "FROM option_chain_snapshots WHERE underlying = :u AND taken_at >= :a "
        "AND taken_at < :b ORDER BY taken_at"
    )
    snaps: dict[str, dict[str, Any]] = {}
    with engine.connect() as c:
        for sid, ts, exp, spot, strike, ot, oi in c.execute(
            q, {"u": underlying, "a": a.isoformat(), "b": (b + dt.timedelta(days=1)).isoformat()}
        ):
            t = ts if isinstance(ts, dt.datetime) else dt.datetime.fromisoformat(str(ts))
            s = snaps.setdefault(
                sid,
                {
                    "ts": t.replace(tzinfo=dt.UTC),
                    "exp": dt.date.fromisoformat(str(exp)),
                    "spot": spot,
                    "put": {},
                    "call": {},
                },
            )
            if oi is not None:
                s["put" if ot == "PE" else "call"][float(strike)] = int(oi)
    by_day: dict[dt.date, set[dt.date]] = {}
    for s in snaps.values():
        by_day.setdefault(to_ist(s["ts"]).date(), set()).add(s["exp"])
    traded: dict[dt.date, dt.date] = {}
    for d, exps in by_day.items():
        ordered = sorted(e for e in exps if e >= d)
        if not ordered:
            continue
        traded[d] = (
            ordered[1]
            if ordered[0] == d and on_expiry_day == "next_weekly" and len(ordered) > 1
            else ordered[0]
        )
    out = []
    for s in sorted(snaps.values(), key=lambda x: x["ts"]):
        d = to_ist(s["ts"]).date()
        if s["spot"] is None or traded.get(d) != s["exp"]:
            continue
        out.append(ChainPoint(s["ts"], float(str(s["spot"])), s["put"], s["call"]))
    return out


def load_dataset(
    engine: Engine,
    underlyings: tuple[str, ...],
    a: dt.date,
    b: dt.date,
    with_options: bool = True,
    on_expiry_day: str = "next_weekly",
) -> Dataset:
    per = {}
    for u in underlyings:
        idx = [
            x
            for x in load_bars(engine, f"INDEX|{u}", "1m", SOURCES, a, b)
            if a <= to_ist(x.ts).date() <= b
        ]
        fut = {x.ts: x for x in load_bars(engine, f"FUTC|{u}", "1m", SOURCES, a, b)}
        days = sorted({to_ist(x.ts).date() for x in idx})
        per[u] = UnderlyingData(
            u,
            idx,
            fut,
            _daily_map(engine, f"INDEX|{u}", idx, a - dt.timedelta(days=10), b),
            days,
            load_chain_points(engine, u, a, b, on_expiry_day),
        )
    vix1 = load_bars(engine, "INDEX|INDIAVIX", "1m", SOURCES, a, b)
    vixd = {
        to_ist(x.ts).date(): x
        for x in load_bars(engine, "INDEX|INDIAVIX", "1d", SOURCES, a - dt.timedelta(days=10), b)
    }
    if not vixd:
        vixd = {to_ist(x.ts).date(): x for x in daily(vix1)}
    opt: dict[str, dict[dt.datetime, Bar]] = defaultdict(dict)
    rec_exp: dict[tuple[str, dt.date], list[dt.date]] = defaultdict(list)
    if with_options:
        with engine.connect() as c:
            keys = [
                r[0]
                for r in c.execute(
                    text(
                        "SELECT DISTINCT instrument_key FROM candles "
                        "WHERE instrument_key LIKE 'OPT|%' "
                        "AND timeframe = '1m'"
                    )
                )
            ]
            for r in c.execute(
                text(
                    "SELECT DISTINCT underlying, master_date, expiry FROM instruments "
                    "WHERE kind = 'OPT' AND master_date BETWEEN :a AND :b"
                ),
                {"a": a.isoformat(), "b": b.isoformat()},
            ):
                rec_exp[(r[0], dt.date.fromisoformat(str(r[1])))].append(
                    dt.date.fromisoformat(str(r[2]))
                )
        for k in keys:
            if k.split("|")[1] in underlyings:
                opt[k] = {x.ts: x for x in load_bars(engine, k, "1m", SOURCES, a, b)}
        for v in rec_exp.values():
            v.sort()
    return Dataset(per, vix1, vixd, dict(opt), dict(rec_exp))
