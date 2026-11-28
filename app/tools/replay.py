"""Replay a recorded session from raw ticks (section 14.1, replay mode).

    python -m app.tools.replay --date 2026-12-02

1. Rebuilds 1-minute candles from the stored ticks with the same CandleBuilder the live
   engine uses, and compares them with the official REST candles for that day.
2. Runs the day through the backtest engine (same views, fill rules and books as the
   backtester; earlier candles are used for warm-up) and writes the resulting ledger.
Writes reports/replay/<date>.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text

from app.adapters.base import TickData
from app.core.timeutil import IST, to_ist
from app.core.types import InstrumentKey
from app.data.bars import Bar
from app.data.candle_builder import CandleBuilder
from app.data.store import load_bars
from app.research.dataset import Dataset

M = dt.timedelta(minutes=1)


def load_ticks(engine: Engine, key: str, day: dt.date) -> list[TickData]:
    a = dt.datetime.combine(day, dt.time(0, 0), IST).astimezone(dt.UTC).replace(tzinfo=None)
    q = text(
        "SELECT exchange_ts, received_ts, ltp, cum_volume FROM ticks WHERE "
        "instrument_key = :k AND exchange_ts >= :a AND exchange_ts < :b ORDER BY id"
    )
    k = _key(key)
    out = []
    with engine.connect() as c:
        for ets, rts, ltp, cum in c.execute(
            q, {"k": key, "a": a.isoformat(" "), "b": (a + dt.timedelta(days=1)).isoformat(" ")}
        ):
            e = ets if isinstance(ets, dt.datetime) else dt.datetime.fromisoformat(str(ets))
            r = rts if isinstance(rts, dt.datetime) else dt.datetime.fromisoformat(str(rts))
            out.append(
                TickData(
                    key=k,
                    exchange_ts=e.replace(tzinfo=dt.UTC),
                    received_ts=r.replace(tzinfo=dt.UTC),
                    ltp=Decimal(str(ltp)),
                    cum_volume=cum,
                )
            )
    return out


def _key(canon: str) -> InstrumentKey:
    kind, *rest = canon.split("|")
    if kind == "INDEX":
        return InstrumentKey.index(rest[0])
    if kind == "FUTC":
        return InstrumentKey.continuous_future(rest[0])
    if kind == "FUT":
        return InstrumentKey.future(rest[0], dt.date.fromisoformat(rest[1]))
    raise ValueError(canon)


def rebuild(ticks: Iterable[TickData]) -> tuple[list[Bar], dict[str, Any]]:
    cb = CandleBuilder()
    bars: list[Bar] = []
    for t in ticks:
        bars += cb.add(t)
    bars += cb.flush()
    s = cb.stats
    return bars, {
        "ticks": s.ticks,
        "duplicates": s.duplicates,
        "late": s.late,
        "gap_minutes": len(s.gap_minutes),
        "candles": s.candles,
    }


def compare(rebuilt: list[Bar], official: list[Bar], tol: float = 0.051) -> dict[str, Any]:
    o = {b.ts: b for b in official}
    r = {b.ts: b for b in rebuilt}
    both = sorted(set(o) & set(r))
    mismatch = [
        ts
        for ts in both
        if any(
            abs(getattr(o[ts], f) - getattr(r[ts], f)) > tol
            for f in ("open", "high", "low", "close")
        )
    ]
    return {
        "official": len(o),
        "rebuilt": len(r),
        "matched_minutes": len(both),
        "missing_in_rebuilt": len(set(o) - set(r)),
        "extra_in_rebuilt": len(set(r) - set(o)),
        "ohlc_mismatches": len(mismatch),
        "first_mismatches": [to_ist(t).strftime("%H:%M") for t in mismatch[:10]],
    }


def replay_dataset(
    ds: Dataset, day: dt.date, rebuilt: dict[str, list[Bar]], rebuilt_fut: dict[str, list[Bar]]
) -> Dataset:
    """Swap the day's candles for the ones rebuilt from ticks; keep history for warm-up."""
    for u, d in ds.per_underlying.items():
        keep = [b for b in d.bars if to_ist(b.ts).date() != day]
        d.bars = keep + rebuilt.get(u, [])
        fut = {ts: b for ts, b in d.futures.items() if to_ist(ts).date() != day}
        fut.update({b.ts: b for b in rebuilt_fut.get(u, [])})
        d.futures = fut
        if rebuilt.get(u) and day not in d.trading_days:
            d.trading_days = sorted([*d.trading_days, day])
    return ds


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--date", required=True, type=dt.date.fromisoformat)
    a = ap.parse_args(argv)
    from app.config.loader import load_config
    from app.db.session import make_engine
    from app.research.dataset import load_dataset
    from app.research.report import trade_rows
    from app.tools.backtest import UNDERLYINGS, WARMUP_DAYS, make_books, run_books

    cfg = load_config()
    engine = make_engine(cfg.database.url)
    report: dict[str, Any] = {"date": a.date.isoformat(), "series": {}}
    rebuilt: dict[str, list[Bar]] = {}
    rebuilt_fut: dict[str, list[Bar]] = {}
    for u in UNDERLYINGS:
        key = f"INDEX|{u}"
        bars, stats = rebuild(load_ticks(engine, key, a.date))
        rebuilt[u] = bars
        official = [
            b
            for b in load_bars(engine, key, "1m", "fyers_hist", a.date, a.date)
            if to_ist(b.ts).date() == a.date
        ]
        report["series"][key] = {**stats, **compare(bars, official)}
        with engine.connect() as c:
            fut_keys = [
                r[0]
                for r in c.execute(
                    text("SELECT DISTINCT instrument_key FROM ticks WHERE instrument_key LIKE :p"),
                    {"p": f"FUT|{u}|%"},
                )
            ]
        for fk in fut_keys:
            fb, fstats = rebuild(load_ticks(engine, fk, a.date))
            rebuilt_fut.setdefault(u, []).extend(fb)
            report["series"][fk] = fstats
    ds = load_dataset(
        engine,
        UNDERLYINGS,
        a.date - dt.timedelta(days=WARMUP_DAYS),
        a.date,
        on_expiry_day=cfg.contracts.nifty_expiry_day,
    )
    ds = replay_dataset(ds, a.date, rebuilt, rebuilt_fut)
    results, _ = run_books(cfg, ds, make_books(cfg), a.date, a.date)
    report["books"] = {
        r.spec.name: {"signals": r.signals, "trades": len(r.trades), "rejected": dict(r.rejected)}
        for r in results
    }
    report["ledger"] = trade_rows(results)
    out = Path("reports") / "replay" / f"{a.date}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report["series"], indent=1))
    print(f"\nWritten {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
