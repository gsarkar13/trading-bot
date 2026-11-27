"""Replay a recorded session from raw ticks (section 14.1, replay mode).

    python -m app.tools.replay --date 2026-12-02

1. Rebuilds 1-minute candles from the stored ticks with the same CandleBuilder the live
   engine uses, and compares them with the official REST candles for that day.
2. Runs both strategy plugins over [earlier candles for warm-up] + [replayed candles] and
   lists the signals they would have produced.
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
from app.strategies.base import MarketView, StrategyPlugin

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


def signals_for(
    plugin: StrategyPlugin,
    underlying: str,
    history: list[Bar],
    day_bars: list[Bar],
    prior_day: Bar | None,
    futures: dict[dt.datetime, Bar],
) -> list[dict[str, Any]]:
    out = []
    for b in history:
        plugin.on_candle("1m", MarketView(underlying, b.ts + M, b, futures_bar=futures.get(b.ts)))
    for b in day_bars:
        for s in plugin.on_candle(
            "1m",
            MarketView(underlying, b.ts + M, b, futures_bar=futures.get(b.ts), prior_day=prior_day),
        ):
            out.append(
                {
                    "at": to_ist(s.at).strftime("%H:%M"),
                    "direction": s.direction,
                    "ref": float(s.reference_price),
                    "stop": float(s.stop_underlying),
                }
            )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--date", required=True, type=dt.date.fromisoformat)
    a = ap.parse_args(argv)
    from app.config.loader import load_config, trade_config_hash
    from app.db.session import make_engine
    from app.strategies.ema200_hammer import Ema200HammerRvolOi
    from app.strategies.pdh_pdl import PdhPdlBreakout

    cfg = load_config()
    engine = make_engine(cfg.database.url)
    report: dict[str, Any] = {"date": a.date.isoformat(), "series": {}, "signals": {}}
    for u in ("NIFTY", "BANKNIFTY"):
        key = f"INDEX|{u}"
        ticks = load_ticks(engine, key, a.date)
        rebuilt, stats = rebuild(ticks)
        official = [
            b
            for b in load_bars(engine, key, "1m", "fyers_hist", a.date, a.date)
            if to_ist(b.ts).date() == a.date
        ]
        report["series"][key] = {**stats, **compare(rebuilt, official)}
        futures = {
            b.ts: b
            for b in load_bars(
                engine, f"FUTC|{u}", "1m", "fyers_hist", a.date - dt.timedelta(days=45), a.date
            )
        }
        with engine.connect() as c:
            fut_keys = [
                r[0]
                for r in c.execute(
                    text("SELECT DISTINCT instrument_key FROM ticks WHERE instrument_key LIKE :p"),
                    {"p": f"FUT|{u}|%"},
                )
            ]
        for fk in fut_keys:  # today's futures candles rebuilt from the recorded ticks
            fbars, _ = rebuild(load_ticks(engine, fk, a.date))
            futures.update({b.ts: b for b in fbars})
        hist = [
            b
            for b in load_bars(
                engine,
                key,
                "1m",
                ("fyers_hist", "fyers_ws"),
                a.date - dt.timedelta(days=45),
                a.date,
            )
            if to_ist(b.ts).date() < a.date
        ]
        prior = None
        if hist:
            last_day = to_ist(hist[-1].ts).date()
            day_hist = [b for b in hist if to_ist(b.ts).date() == last_day]
            prior = Bar(
                day_hist[0].ts,
                day_hist[0].open,
                max(b.high for b in day_hist),
                min(b.low for b in day_hist),
                day_hist[-1].close,
            )
        for plugin in (
            Ema200HammerRvolOi(
                cfg.strategy_params("ema200_hammer_rvol_oi"),
                trade_config_hash(cfg, "ema200_hammer_rvol_oi"),
            ),
            PdhPdlBreakout(
                cfg.strategy_params("pdh_pdl_breakout"), trade_config_hash(cfg, "pdh_pdl_breakout")
            ),
        ):
            report["signals"][f"{plugin.id} {u}"] = signals_for(
                plugin, u, hist, rebuilt, prior, futures
            )
    out = Path("reports") / "replay" / f"{a.date}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report["series"], indent=1))
    print(f"\nWritten {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
