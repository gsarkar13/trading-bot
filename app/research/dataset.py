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
        """Latest VIX close at or before ts; else the previous day's daily close."""
        i = bisect.bisect_right(self._vix_ts, ts) - 1
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


def load_chain_points(engine: Engine, underlying: str, a: dt.date, b: dt.date) -> list[ChainPoint]:
    q = text(
        "SELECT snapshot_id, taken_at, expiry, spot, strike, option_type, oi "
        "FROM option_chain_snapshots WHERE underlying = :u AND taken_at >= :a "
        "AND taken_at < :b ORDER BY taken_at"
    )
    snaps: dict[str, dict[str, object]] = {}
    with engine.connect() as c:
        for sid, ts, exp, spot, strike, ot, oi in c.execute(
            q, {"u": underlying, "a": a.isoformat(), "b": (b + dt.timedelta(days=1)).isoformat()}
        ):
            s = snaps.setdefault(sid, {"ts": ts, "exp": exp, "spot": spot, "put": {}, "call": {}})
            if oi is not None:
                side = "put" if ot == "PE" else "call"
                s[side][float(strike)] = int(oi)  # type: ignore[index]
    # Keep the nearest expiry of each snapshot round only (the contract that is traded).
    by_round: dict[str, dict[str, object]] = {}
    for s in snaps.values():
        k = str(s["ts"])[:19]
        if k not in by_round or str(s["exp"]) < str(by_round[k]["exp"]):
            by_round[k] = s
    out = []
    for s in sorted(by_round.values(), key=lambda x: str(x["ts"])):
        ts = (
            s["ts"] if isinstance(s["ts"], dt.datetime) else dt.datetime.fromisoformat(str(s["ts"]))
        )
        if s["spot"] is None:
            continue
        put, call = s["put"], s["call"]
        assert isinstance(put, dict) and isinstance(call, dict)
        out.append(ChainPoint(ts.replace(tzinfo=dt.UTC), float(str(s["spot"])), put, call))
    return out


def load_dataset(
    engine: Engine, underlyings: tuple[str, ...], a: dt.date, b: dt.date, with_options: bool = True
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
            load_chain_points(engine, u, a, b),
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
