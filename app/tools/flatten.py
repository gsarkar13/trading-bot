"""Standalone emergency flatten (brief sections 4 inv 11, 13.2).

    python -m app.tools.flatten --confirm FLATTEN

For when the engine is hung or dead. It takes the instance lock first, so it can never
run beside a live engine: the watchdog stops the engine process, then runs this.

For each account (every paper book on disk; the execution broker from Phase 3):
  1. cancel every working order
  2. sell each long position with a marketable LIMIT (bid - 2 ticks; never a market order),
     re-priced toward the bid until flat or the attempts run out
  3. report what is still open, loudly, so the trader exits by hand
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.adapters.base import (
    BaseExecutionBroker,
    BrokerOrderStatus,
    InstrumentInfo,
    OrderRequest,
)
from app.brokers.paper import Depth, PaperBroker, PaperConfig
from app.core.types import InstrumentKey, OrderType, Side
from app.db.models import Instrument
from app.engine.reconcile import TagSource

QuoteFn = Callable[[InstrumentKey], Awaitable[Depth | None]]
WORKING = (BrokerOrderStatus.PENDING, BrokerOrderStatus.TRIGGER_PENDING, BrokerOrderStatus.PARTIAL)


@dataclass
class FlattenReport:
    account: str
    cancelled: int = 0
    sold: list[str] = field(default_factory=list)
    still_open: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"{self.account}: cancelled {self.cancelled} order(s), closed {len(self.sold)}"]
        out += [f"  closed {s}" for s in self.sold]
        out += [f"  STILL OPEN - EXIT BY HAND: {s}" for s in self.still_open]
        return out


def infos_from_db(engine: Engine, keys: list[InstrumentKey]) -> dict[InstrumentKey, InstrumentInfo]:
    out: dict[InstrumentKey, InstrumentInfo] = {}
    with Session(engine) as s:
        for k in keys:
            r = s.scalar(
                select(Instrument)
                .where(Instrument.key == k.canonical)
                .order_by(Instrument.master_date.desc())
            )
            if r is not None and r.lot_size and r.tick_size:
                out[k] = InstrumentInfo(
                    key=k,
                    broker_symbol=r.broker_symbol,
                    broker_token=r.broker_token,
                    lot_size=r.lot_size,
                    tick_size=r.tick_size,
                    freeze_qty=r.freeze_qty,
                )
    return out


def _round_down(p: Decimal, tick: Decimal) -> Decimal:
    return max(tick, (p / tick).to_integral_value(rounding="ROUND_FLOOR") * tick)


async def flatten_broker(
    broker: BaseExecutionBroker,
    name: str,
    instance_id: str,
    today: dt.date,
    quote: QuoteFn,
    infos: dict[InstrumentKey, InstrumentInfo],
    attempts: int = 10,
    pause_s: float = 1.0,
    feed: Callable[[InstrumentKey, Depth], None] | None = None,
) -> FlattenReport:
    rep = FlattenReport(name)
    book = await broker.order_book()
    for o in book:
        if o.status in WORKING:
            r = await broker.cancel_order(o.broker_order_id)
            rep.cancelled += r.outcome == "ACCEPTED"
    tags = TagSource(instance_id, today, [o.client_tag for o in book if o.client_tag])
    for _ in range(attempts):
        longs = [p for p in await broker.positions() if p.key is not None and p.net_qty > 0]
        if not longs:
            break
        for o in await broker.order_book():  # last round's unfilled exits
            if o.status in WORKING:
                await broker.cancel_order(o.broker_order_id)
        for p in longs:
            assert p.key is not None
            info = infos.get(p.key)
            q = await quote(p.key)
            if q is not None and feed is not None:
                feed(p.key, q)
            ref = (q.bid or q.ltp) if q else None
            if info is None or ref is None:
                continue
            px = _round_down(ref - 2 * info.tick_size, info.tick_size)
            res = await broker.place_order(
                OrderRequest(
                    client_tag=tags(),
                    key=p.key,
                    side=Side.SELL,
                    qty=p.net_qty,
                    order_type=OrderType.LMT,
                    limit_price=px,
                    product=p.product,
                )
            )
            if res.outcome == "ACCEPTED":
                rep.sold.append(f"{p.key.canonical} x{p.net_qty} @ <= {px}")
        await asyncio.sleep(pause_s)
    rep.still_open = [
        f"{p.key.canonical if p.key else p.broker_symbol} x{p.net_qty}"
        for p in await broker.positions()
        if p.net_qty > 0
    ]
    return rep


async def flatten_paper_books(
    state_dir: Path,
    engine: Engine,
    instance_id: str,
    today: dt.date,
    quote: QuoteFn,
    now: Callable[[], dt.datetime],
    pause_s: float = 1.0,
) -> list[FlattenReport]:
    out = []
    for f in sorted(state_dir.glob("*.json")):
        probe = PaperBroker({}, now, PaperConfig(latency_s=0.0), state_path=f)
        keys = list({o.key for o in await probe.order_book() if o.key is not None})
        b = PaperBroker(infos_from_db(engine, keys), now, PaperConfig(latency_s=0.0), state_path=f)
        out.append(
            await flatten_broker(
                b,
                f.stem.replace("_", ":", 1),
                instance_id,
                today,
                quote,
                b.instruments,
                pause_s=pause_s,
                feed=b.on_market,
            )
        )
    return out


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - needs a login
    from app.config.loader import load_config
    from app.core.timeutil import IST
    from app.db.session import make_engine
    from app.engine.instance import AlreadyRunning, InstanceLock

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--confirm", required=True, help="type FLATTEN")
    a = ap.parse_args(argv)
    if a.confirm != "FLATTEN":
        print("Nothing done: pass --confirm FLATTEN")
        return 2
    cfg = load_config()
    lock = InstanceLock(Path("data") / f"engine-{cfg.instance.instance_id}.lock")
    try:
        lock.acquire()
    except AlreadyRunning:
        print(
            "The engine is still running: use the dashboard/Telegram flatten, or stop "
            "the engine first."
        )
        return 4
    db = make_engine(cfg.database.url, cfg.database.sqlite_busy_timeout_ms)
    quote = _fyers_quote_fn(cfg)
    reps = asyncio.run(
        flatten_paper_books(
            Path("data") / "paper",
            db,
            cfg.instance.instance_id,
            dt.datetime.now(IST).date(),
            quote,
            lambda: dt.datetime.now(dt.UTC),
        )
    )
    for r in reps:
        print("\n".join(r.lines()))
    lock.release()
    return 1 if any(r.still_open for r in reps) else 0


def _fyers_quote_fn(cfg: object) -> QuoteFn:  # pragma: no cover - live data
    from app.adapters.fyers.session import load_token, make_client, open_vault
    from app.adapters.fyers.symbols import SymbolMap, download_master, parse_master
    from app.config.schema import PlatformConfig
    from app.config.secrets import Secrets

    assert isinstance(cfg, PlatformConfig)
    client = make_client(cfg, load_token(open_vault(Secrets())))
    smap = SymbolMap(parse_master(download_master("NSE_FO")))

    async def q(key: InstrumentKey) -> Depth | None:
        sym = smap.symbol(key)
        resp = await asyncio.to_thread(client.quotes, [sym])
        for row in resp.get("d", []):
            v = row.get("v", {})
            if row.get("n") == sym and v.get("bid"):
                return Depth(
                    Decimal(str(v["bid"])),
                    Decimal(str(v.get("ask") or v["bid"])),
                    10**6,
                    10**6,
                    Decimal(str(v.get("lp") or v["bid"])),
                    dt.datetime.now(dt.UTC),
                )
        return None

    return q


if __name__ == "__main__":
    sys.exit(main())
