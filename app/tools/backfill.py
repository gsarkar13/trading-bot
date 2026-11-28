"""Download history from Fyers into the local database (section 16.4).

    python -m app.tools.backfill --from 2026-05-01 --to 2026-11-27

Series: NIFTY and BANKNIFTY spot (1-minute + daily), India VIX (1-minute + daily) and the
continuous front-month future of each index (1-minute + daily; this is where volume
comes from, because indices have none).

* Resumable: each finished request is recorded in `backfill_chunks`; re-running skips it.
* Rate limited (config `rate_limits`), and refuses to run 09:00-15:45 IST on weekdays
  unless --force, so it never competes with the recorder for the daily request quota.
* Fyers Standard plan keeps only the last ~1 year of intraday data (DECISIONS.md D23).
* Writes reports/backfill/summary_<from>_<to>.json; attach it in the chat.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.adapters.base import (
    AdapterError,
    AuthExpiredError,
    InstrumentMappingError,
    RateLimitedError,
)
from app.adapters.fyers.client import MAX_DAILY_DAYS, MAX_INTRADAY_DAYS, chunks
from app.core.timeutil import IST, to_ist
from app.core.types import InstrumentKey
from app.data.bars import Bar
from app.data.store import load_bars, upsert_bars
from app.db.models import BackfillChunk

SOURCE = "fyers_hist"
Fetch = Callable[[str, str, dt.date, dt.date, bool], list[Bar]]


@dataclass(frozen=True)
class Series:
    key: InstrumentKey
    symbol: str
    cont: bool

    @property
    def name(self) -> str:
        return self.key.canonical


def default_series(front_future_symbols: dict[str, str]) -> list[Series]:
    out = [
        Series(InstrumentKey.index("NIFTY"), "NSE:NIFTY50-INDEX", False),
        Series(InstrumentKey.index("BANKNIFTY"), "NSE:NIFTYBANK-INDEX", False),
        Series(InstrumentKey.index("INDIAVIX"), "NSE:INDIAVIX-INDEX", False),
    ]
    for u, sym in front_future_symbols.items():
        out.append(Series(InstrumentKey.continuous_future(u), sym, True))
    return out


def market_hours(now: dt.datetime) -> bool:
    t = to_ist(now)
    return t.weekday() < 5 and dt.time(9, 0) <= t.time() <= dt.time(15, 45)


def run_backfill(
    engine: Engine,
    fetch: Fetch,
    series: list[Series],
    start: dt.date,
    end: dt.date,
    today: dt.date,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    summary: dict[str, Any] = {"from": start.isoformat(), "to": end.isoformat(), "series": {}}
    for s in series:
        for tf, res, limit in (("1m", "1", MAX_INTRADAY_DAYS), ("1d", "D", MAX_DAILY_DAYS)):
            info = summary["series"].setdefault(
                f"{s.name} {tf}", {"fetched": 0, "skipped": 0, "errors": []}
            )
            for a, b in chunks(start, end, limit):
                with Session(engine) as db:
                    done = db.scalar(
                        select(BackfillChunk.id).where(
                            BackfillChunk.series == s.name,
                            BackfillChunk.timeframe == tf,
                            BackfillChunk.start == a,
                            BackfillChunk.end == b,
                            BackfillChunk.source == SOURCE,
                        )
                    )
                if done:
                    info["skipped"] += 1
                    continue
                for attempt in range(4):
                    try:
                        bars = fetch(s.symbol, res, a, b, s.cont)
                        break
                    except RateLimitedError:
                        wait = 15 * (attempt + 1)
                        log(f"  rate limited; waiting {wait}s")
                        sleep(wait)
                    except AuthExpiredError:
                        raise  # nothing else will work either; stop and say so
                    except AdapterError as e:
                        # e.g. a range older than the plan allows: note it, keep going
                        info["errors"].append(f"{a}..{b}: {e}")
                        log(f"  {s.name} {tf} {a}..{b}: refused by Fyers: {e}")
                        bars = None
                        break
                else:
                    info["errors"].append(f"{a}..{b}: rate limited 4 times")
                    continue
                if bars is None:
                    continue
                n = upsert_bars(engine, s.name, tf, SOURCE, bars)
                info["fetched"] += n
                log(f"  {s.name} {tf} {a}..{b}: {n} bars")
                if b < today:  # today's chunk may still grow; never mark it finished
                    with Session(engine) as db:
                        db.add(
                            BackfillChunk(
                                series=s.name,
                                timeframe=tf,
                                start=a,
                                end=b,
                                rows=n,
                                source=SOURCE,
                                done_at=dt.datetime.now(dt.UTC),
                            )
                        )
                        db.commit()
    for s in series:
        bars = load_bars(engine, s.name, "1m", SOURCE, start, end)
        days = sorted({to_ist(b.ts).date() for b in bars if start <= to_ist(b.ts).date() <= end})
        info = summary["series"][f"{s.name} 1m"]
        info.update(
            {
                "total_bars": len(bars),
                "days": len(days),
                "first": days[0].isoformat() if days else None,
                "last": days[-1].isoformat() if days else None,
                "bars_per_day_min": min(
                    (sum(1 for b in bars if to_ist(b.ts).date() == d) for d in days), default=0
                ),
            }
        )
    idx_days = {d for d in _days(engine, "INDEX|NIFTY", start, end)}
    for s in series:
        if s.key.kind == "FUTC":
            missing = sorted(idx_days - set(_days(engine, s.name, start, end)))
            summary["series"][f"{s.name} 1m"]["days_missing_vs_index"] = [
                d.isoformat() for d in missing[:50]
            ]
    return summary


def _days(engine: Engine, key: str, a: dt.date, b: dt.date) -> list[dt.date]:
    return sorted(
        {
            to_ist(x.ts).date()
            for x in load_bars(engine, key, "1m", SOURCE, a, b)
            if a <= to_ist(x.ts).date() <= b
        }
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--from", dest="start", required=True, type=dt.date.fromisoformat)
    ap.add_argument("--to", dest="end", required=True, type=dt.date.fromisoformat)
    ap.add_argument("--force", action="store_true", help="allow running in market hours")
    a = ap.parse_args(argv)
    now = dt.datetime.now(dt.UTC)
    if market_hours(now) and not a.force:
        print("Refusing to backfill during market hours (09:00-15:45 IST); use --force.")
        return 2
    from app.adapters.fyers.session import load_token, make_client, open_vault
    from app.adapters.fyers.symbols import SymbolMap, download_master, parse_master
    from app.config.loader import load_config
    from app.config.secrets import Secrets
    from app.db.session import make_engine
    from app.tools.init_db import main as init_db

    init_db()
    cfg = load_config()
    try:
        client = make_client(cfg, load_token(open_vault(Secrets())))
    except AuthExpiredError as e:
        print(e)
        return 2
    today = now.astimezone(IST).date()
    smap = SymbolMap(parse_master(download_master("NSE_FO")))
    futs = {u: smap.symbol(smap.front_future(u, today, 0)) for u in ("NIFTY", "BANKNIFTY")}
    engine = make_engine(cfg.database.url, cfg.database.sqlite_busy_timeout_ms)

    def fetch(sym: str, res: str, x: dt.date, y: dt.date, cont: bool) -> list[Bar]:
        return client.history(sym, res, x, y, cont_flag=cont)

    try:
        summary = run_backfill(engine, fetch, default_series(futs), a.start, a.end, today)
    except AuthExpiredError as e:
        print(f"{e}\nLog in again and re-run; finished parts are kept.")
        return 1
    except InstrumentMappingError as e:
        print(f"Fyers rejected a symbol: {e}")
        return 1
    out = Path("reports") / "backfill" / f"summary_{a.start}_{a.end}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"\nDone. Attach {out} in the chat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
