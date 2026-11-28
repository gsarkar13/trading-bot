"""Chaos: the engine is killed (no clean shutdown) with positions open, then restarted.

Invariant 7: on restart the broker is the truth; positions are resumed, and anything held
without a working stop is UNMANAGED and gets protected (or flattened) with a critical alert.
Invariant 10: a restart comes back unarmed. Invariant 16: foreign orders stop entries.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.adapters.base import InstrumentInfo, OrderRequest
from app.brokers.paper import Depth, PaperBroker, PaperConfig
from app.core.types import OrderType, PositionState, Product, Side
from app.db.models import Trade
from tests.engine_harness import OPT, Clock, Harness, activate, ist

D = Decimal


def open_then_kill(db: Engine, tmp: Path) -> Harness:
    activate(db)
    h = Harness(db, tmp)
    h.strategy.script[ist(10, 0).astimezone(dt.UTC)] = ("LONG_CE", 24000, 23985, 24030)
    asyncio.run(h.minute(10, 0, 24000))
    assert len(h.engine.positions) == 2
    return h  # dropped without any shutdown: the "kill -9"


def restart(db: Engine, tmp: Path, at: dt.datetime, live: PaperBroker | None = None) -> Harness:
    h = Harness(db, tmp, clock=Clock(at), process_id="proc-2", live=live)
    asyncio.run(h.engine.start())
    return h


def test_restart_resumes_protected_positions_and_exits_on_target(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    open_then_kill(migrated_engine, tmp_path)
    h = restart(migrated_engine, tmp_path, ist(10, 3))
    ps = list(h.engine.positions.values())
    assert {p.book for p in ps} == {"SHADOW:pdh_pdl_breakout", "PAPER"}
    assert all(p.machine.state is PositionState.OPEN_PROTECTED for p in ps)
    assert all(p.stop == 23985 and p.target == 24030 for p in ps)
    assert not h.engine.arming.is_armed(h.clock())  # inv 10: back unarmed

    async def go() -> None:
        await h.option("131.00", "131.40")
        await h.index(24031, ist(10, 3, 5))
        await h.engine.on_timer()

    asyncio.run(go())
    assert not h.engine.positions
    with Session(migrated_engine) as s:
        assert len(list(s.scalars(select(Trade)))) == 2
    tags = [o.tag for p in ps for o in p.machine.orders.values()]
    assert len(tags) == len(set(tags))  # inv 5: new tags never reuse old ones


def test_missing_stop_after_restart_is_unmanaged_then_protected(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    open_then_kill(migrated_engine, tmp_path)
    f = tmp_path / "PAPER.json"
    raw = json.loads(f.read_text())
    for o in raw["orders"]:
        if o["order_type"] == "SL-LMT":
            o["status"] = "CANCELLED"  # the stop vanished while we were down
    f.write_text(json.dumps(raw))
    h = restart(migrated_engine, tmp_path, ist(10, 3))
    crit = [a.message for a in h.alerts.recent if a.level == "critical"]
    assert any("no working stop" in m for m in crit)
    [p] = [p for p in h.engine.positions.values() if p.book == "PAPER"]
    assert p.machine.state is PositionState.OPEN_PROTECTED
    prot = p.machine.protect
    assert prot is not None and prot.order_type is OrderType.SL_LMT
    states = [t.to_state for t in p.machine.transitions]
    assert "UNMANAGED" in states


def test_unknown_broker_position_is_adopted_and_protected(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    activate(migrated_engine)
    clock = Clock(ist(10, 3))
    h0 = Harness(migrated_engine, tmp_path, clock=clock)
    info: InstrumentInfo = h0.engine.smap.info(OPT)
    live = PaperBroker({OPT: info}, clock, PaperConfig(latency_s=0.0))
    live.on_market(OPT, Depth(D("120.00"), D("120.50"), 650, 650, D("120.2"), clock()))
    # a manual trade in the real account, tagged by this instance but unknown to the DB
    live.place_sync(
        OrderRequest(
            client_tag="LAP1261202000090",
            key=OPT,
            side=Side.BUY,
            qty=65,
            order_type=OrderType.LMT,
            limit_price=D("121.00"),
            product=Product.MIS,
        )
    )
    h = restart(migrated_engine, tmp_path, ist(10, 3), live=live)
    notes = [a.message for a in h.alerts.recent]
    assert any("no record here" in m for m in notes)
    r = h.engine.routers["live"]
    [t] = r.positions.values()
    assert t.machine.protect is not None and t.machine.long_qty == 65
    # tags continue after the highest one seen at the broker (inv 5)
    assert t.machine.protect.tag == "LAP1261202000091"


def test_foreign_instance_order_stops_new_entries(migrated_engine: Engine, tmp_path: Path) -> None:
    h = open_then_kill(migrated_engine, tmp_path)
    b = h.engine.paper["PAPER"]
    b.place_sync(
        OrderRequest(
            client_tag="ZZ261202000001",
            key=OPT,
            side=Side.BUY,
            qty=65,
            order_type=OrderType.LMT,
            limit_price=D("100.00"),
            product=Product.MIS,
        )
    )
    h.at(ist(10, 3))  # the check runs once a minute
    asyncio.run(h.engine.on_timer())
    assert h.engine.state.halted and "another instance" in h.engine.state.halted
