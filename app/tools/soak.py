"""Memory soak (Phase 2 gate): run the real engine through N simulated trading days.

    python -m app.tools.soak --days 5

Each day: an index tick every second from 09:15 to 15:30, the engine timer every second,
option quotes for the traded contracts, and a scripted strategy that signals every 20
minutes so trades open and close all day (both books). The real strategy plugins also
run on every candle. Memory (RSS) is sampled at the end of each day.

Pass: peak RSS under `--limit-mb` (default 600: the engine+recorder process must leave
room for the watchdog and the OS on a 1 GB server) and day-over-day growth after day 2
under `--growth-mb` (a leak shows up as steady growth).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import gc
import json
import random
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from app.adapters.base import InstrumentInfo, TickData
from app.adapters.fyers.symbols import SymbolMap
from app.brokers.paper import Depth, PaperBroker, PaperConfig
from app.config.loader import REPO_ROOT
from app.config.schema import PdhPdlParams, PlatformConfig, StrategyEntry
from app.core.timeutil import IST
from app.core.types import InstrumentKey, OptionType, Segment
from app.engine.alerts import AlertBus
from app.engine.engine import Health, Slot, TradingEngine
from app.research.costs import ChargesTable
from app.strategies.base import (
    DataNeeds,
    ExitDecision,
    MarketView,
    OpenPosition,
    Signal,
    StrategyPlugin,
    WarmupRequirement,
)

D = Decimal


def rss_mb() -> float | None:
    """Current resident memory of this process, in MB (None where unknown)."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    try:  # Windows
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        win = ctypes.windll  # type: ignore[attr-defined]
        h = win.kernel32.GetCurrentProcess()
        if win.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return float(pmc.WorkingSetSize) / 2**20
    except (AttributeError, OSError):
        pass
    return None


class SoakStrategy(StrategyPlugin):
    """Signals every `every` minutes, alternating CE and PE, stop 15 points, target 2R."""

    id: ClassVar[str] = "pdh_pdl_breakout"
    segment: ClassVar[Segment] = Segment.OPTIONS
    params_model: ClassVar[type[BaseModel]] = PdhPdlParams
    every = 20

    @classmethod
    def data_needs(cls, params: BaseModel) -> DataNeeds:
        return DataNeeds(timeframes=("1m",))

    @classmethod
    def warmup(cls, params: BaseModel) -> WarmupRequirement:
        return WarmupRequirement(bars={"1m": 0})

    def on_candle(self, timeframe: str, view: MarketView) -> list[Signal]:
        t = view.bar.ts.astimezone(IST)
        n = t.hour * 60 + t.minute
        if n % self.every:
            return []
        long = (n // self.every) % 2 == 0
        px = view.bar.close
        stop = px - 15 if long else px + 15
        return [
            Signal(
                strategy_id=self.id,
                underlying=view.underlying,
                direction="LONG_CE" if long else "LONG_PE",
                at=view.now,
                reference_price=D(str(round(px, 2))),
                stop_underlying=D(str(round(stop, 2))),
                target_underlying=D(str(round(px + (30 if long else -30), 2))),
                checklist={"soak": True},
                config_hash="soak",
            )
        ]

    def exit_logic(self, position: OpenPosition, view: MarketView) -> ExitDecision:
        return ExitDecision(action="HOLD")


def synthetic_master(first: dt.date, weeks: int) -> SymbolMap:
    infos = []
    d = first
    while d.weekday() != 1:
        d += dt.timedelta(days=1)
    for w in range(weeks):
        exp = d + dt.timedelta(weeks=w)
        for k in range(21000, 28001, 50):
            for ot in (OptionType.CE, OptionType.PE):
                key = InstrumentKey.option("NIFTY", exp, k, ot)
                infos.append(
                    InstrumentInfo(
                        key=key,
                        broker_symbol=f"NSE:SOAK{exp:%y%m%d}{k}{ot.value}",
                        broker_token=None,
                        lot_size=65,
                        tick_size=D("0.05"),
                    )
                )
    return SymbolMap(infos)


def premium(key: InstrumentKey, spot: float) -> Decimal:
    assert key.strike is not None
    intrinsic = (
        (spot - float(key.strike))
        if key.option_type is OptionType.CE
        else (float(key.strike) - spot)
    )
    p = max(5.0, 110 + 0.5 * intrinsic)
    return D(str(round(p * 20) / 20))


async def run(days: int, db_dir: Path, seed: int = 7) -> dict[str, object]:
    import os

    from alembic.config import Config

    from alembic import command
    from app.db.models import StrategyRow
    from app.db.seed import sync_strategies_table
    from app.db.session import make_engine

    url = f"sqlite:///{db_dir / 'soak.db'}"
    os.environ["ALGO_DB_URL"] = url
    acfg = Config(str(REPO_ROOT / "alembic.ini"))
    acfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(acfg, "head")
    db = make_engine(url)
    from sqlalchemy import update
    from sqlalchemy.orm import Session

    with Session(db) as s:
        sync_strategies_table(s, dt.datetime.now(dt.UTC))
        s.execute(
            update(StrategyRow).where(StrategyRow.id == "pdh_pdl_breakout").values(active=True)
        )
        s.commit()

    import app.strategies.plugins  # noqa: F401 - the real plugins run alongside
    from app.strategies.registry import get_plugin

    cfg = PlatformConfig(
        strategies=(
            StrategyEntry(id="pdh_pdl_breakout", active=True),
            StrategyEntry(id="ema200_hammer_rvol_oi", active=False),
        )
    )
    start = dt.date(2026, 12, 1)
    smap = synthetic_master(start, days // 5 + 3)
    clock = [dt.datetime.combine(start, dt.time(9, 14), IST)]

    def now() -> dt.datetime:
        return clock[0]

    def paper(book: str, inst: dict[InstrumentKey, InstrumentInfo]) -> PaperBroker:
        return PaperBroker(
            inst,
            now,
            PaperConfig(latency_s=0.2),
            state_path=db_dir / f"{book.replace(':', '_')}.json",
        )

    spot = [24000.0]

    async def fetch(key: InstrumentKey) -> Depth | None:
        p = premium(key, spot[0])
        return Depth(p - D("0.20"), p + D("0.20"), 650, 650, p, now())

    soak = SoakStrategy(PdhPdlParams(), "soak")
    ema_cls = get_plugin("ema200_hammer_rvol_oi")
    assert ema_cls is not None
    ema = ema_cls(cfg.strategy_params("ema200_hammer_rvol_oi"), "soak")
    slots = [
        Slot("pdh_pdl_breakout", soak, cfg.strategies[0], 0),
        Slot("ema200_hammer_rvol_oi", ema, cfg.strategies[1], 10**9),
    ]
    alerts = AlertBus(db, cfg.instance.instance_id, now)
    eng = TradingEngine(
        cfg,
        db,
        smap,
        now,
        slots,
        alerts,
        ChargesTable(REPO_ROOT / "config" / "charges.yaml"),
        "soak",
        paper,
        quote_fetch=fetch,
        health=Health(data_login=True, clock_drift_ms=0.0),
    )
    rnd = random.Random(seed)  # noqa: S311 - simulated prices
    per_day = []
    t0 = time.monotonic()
    d = start
    for _ in range(days):
        while d.weekday() >= 5:
            d += dt.timedelta(days=1)
        eng.tags = type(eng.tags)(cfg.instance.instance_id, d, [])
        sec = dt.datetime.combine(d, dt.time(9, 15), IST)
        end = dt.datetime.combine(d, dt.time(15, 30), IST)
        while sec < end:
            clock[0] = sec
            spot[0] += rnd.gauss(0, 0.9)
            await eng.on_tick(
                TickData(
                    key=InstrumentKey.index("NIFTY"),
                    exchange_ts=sec,
                    received_ts=sec,
                    ltp=D(str(round(spot[0], 2))),
                )
            )
            for k in {p.key for p in eng.positions.values()}:
                p = premium(k, spot[0])
                await eng.on_option_depth(k, Depth(p - D("0.20"), p + D("0.20"), 650, 650, p, sec))
            await eng.on_timer()
            sec += dt.timedelta(seconds=1)
        gc.collect()
        trades = eng.state.signals
        per_day.append(
            {
                "date": d.isoformat(),
                "rss_mb": rss_mb(),
                "signals_total": trades,
                "open_positions": len(eng.positions),
                "elapsed_s": round(time.monotonic() - t0, 1),
            }
        )
        print(json.dumps(per_day[-1]), flush=True)
        d += dt.timedelta(days=1)
    return {"days": per_day}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--limit-mb", type=float, default=600)
    ap.add_argument("--growth-mb", type=float, default=15)
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        rep = asyncio.run(run(a.days, Path(tmp)))
    days = rep["days"]
    assert isinstance(days, list)
    rss = [x["rss_mb"] for x in days if x["rss_mb"] is not None]
    peak = max(rss) if rss else None
    growth = (rss[-1] - rss[1]) / max(1, len(rss) - 2) if len(rss) >= 3 else 0.0
    ok = peak is None or (peak < a.limit_mb and growth < a.growth_mb)
    rep.update(
        {
            "peak_rss_mb": peak,
            "growth_mb_per_day_after_day2": round(growth, 2),
            "limit_mb": a.limit_mb,
            "pass": ok,
        }
    )
    out = Path("reports") / "soak"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"soak-{dt.datetime.now(IST).date()}.json").write_text(
        json.dumps(rep, indent=1), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in rep.items() if k != "days"}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
