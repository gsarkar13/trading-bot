"""Read and write candles. Re-writing a candle replaces it, so backfills can be re-run
safely and a candle stored while still forming is corrected by the next fetch."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from decimal import Decimal

from sqlalchemy import Engine, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.data.bars import Bar
from app.db.models import Candle


def _dec(x: float) -> Decimal:
    return Decimal(repr(round(x, 4)))


def upsert_bars(engine: Engine, key: str, timeframe: str, source: str, bars: Iterable[Bar]) -> int:
    rows = [
        {
            "instrument_key": key,
            "timeframe": timeframe,
            "ts": b.ts,
            "open": _dec(b.open),
            "high": _dec(b.high),
            "low": _dec(b.low),
            "close": _dec(b.close),
            "volume": int(b.volume),
            "oi": b.oi,
            "source": source,
        }
        for b in bars
    ]
    if not rows:
        return 0
    ins = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
    base = ins(Candle)
    # A re-fetch must be able to correct a candle stored while it was still forming
    # (e.g. today's daily bar fetched mid-day), so the newest write wins.
    stmt = base.on_conflict_do_update(
        index_elements=["instrument_key", "timeframe", "ts", "source"],
        set_={c: base.excluded[c] for c in ("open", "high", "low", "close", "volume", "oi")},
    )
    with engine.begin() as conn:
        for i in range(0, len(rows), 2000):
            conn.execute(stmt, rows[i : i + 2000])
    return len(rows)


def load_bars(
    engine: Engine,
    key: str,
    timeframe: str,
    source: str | Sequence[str],
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> list[Bar]:
    """Bars ordered by time. Dates are UTC-day bounds (widened by a day to cover IST)."""
    sources = [source] if isinstance(source, str) else list(source)
    # Only placeholder NAMES (:s0, :s1, ...) are joined in; every value is a bound parameter.
    names = ",".join(f":s{i}" for i in range(len(sources)))
    q = "SELECT ts, open, high, low, close, volume, oi, source FROM candles "
    q += "WHERE instrument_key = :k AND timeframe = :tf AND source IN (" + names + ")"
    params: dict[str, object] = {
        "k": key,
        "tf": timeframe,
        **{f"s{i}": s for i, s in enumerate(sources)},
    }
    if start:
        q += " AND ts >= :a"
        params["a"] = (start - dt.timedelta(days=1)).isoformat() + " 00:00:00"
    if end:
        q += " AND ts < :b"
        params["b"] = (end + dt.timedelta(days=2)).isoformat() + " 00:00:00"
    q += " ORDER BY ts"
    out: dict[dt.datetime, Bar] = {}
    rank: dict[dt.datetime, int] = {}
    with engine.connect() as conn:
        for ts, o, h, lo, c, v, oi, src in conn.execute(text(q), params):
            t = ts if isinstance(ts, dt.datetime) else dt.datetime.fromisoformat(str(ts))
            t = t.replace(tzinfo=dt.UTC)
            r = sources.index(src)
            if t in rank and rank[t] <= r:  # earlier-listed source wins on overlap
                continue
            rank[t] = r
            out[t] = Bar(t, float(o), float(h), float(lo), float(c), int(v), oi)
    return [out[t] for t in sorted(out)]
