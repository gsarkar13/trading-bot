"""SEMI order tasks (section 5.4, invariant 15): verified, never assumed."""

from __future__ import annotations

import asyncio
import itertools
from decimal import Decimal

from app.brokers.semi import SemiBroker, SemiTask, semi_mechanics
from app.config.schema import ExecutionMechanics
from app.core.types import PositionState
from app.engine.oms import Alert, PositionMachine, Quote
from app.engine.router import MemoryStore, OrderRouter
from tests.test_paper_router import INFO, KEY, Clock

D = Decimal


def setup() -> tuple[
    Clock, SemiBroker, OrderRouter, PositionMachine, list[tuple[str, str]], list[Alert]
]:
    c = Clock()
    seen: list[tuple[str, str]] = []
    alerts: list[Alert] = []

    def on_task(t: SemiTask, ev: str) -> None:
        seen.append((t.uid, ev))

    b = SemiBroker(INFO, c, on_task, alerts.append, protect_timeout_s=120)
    r = OrderRouter(b, MemoryStore(), c, alerts.append)
    seq = itertools.count(1)
    m = PositionMachine(
        "P1",
        65,
        D("0.05"),
        65,
        D("100.00"),
        semi_mechanics(ExecutionMechanics()),
        lambda: f"PC1261202{next(seq):06d}",
    )
    r.track(m, KEY)
    return c, b, r, m, seen, alerts


def start(c: Clock, b: SemiBroker, r: OrderRouter, m: PositionMachine) -> str:
    cmds = m.start_entry(c(), Quote(D("120.00"), D("120.50"), D("120.2"), c()))
    b.plan_stop(cmds[0].tag, D("100.00"), D("99.50"))  # type: ignore[union-attr]
    asyncio.run(r.execute("P1", cmds))
    [uid] = b.tasks
    return uid


def test_one_instruction_shows_entry_and_its_stop_and_nothing_is_assumed() -> None:
    c, b, r, m, seen, _ = setup()
    uid = start(c, b, r, m)
    msg = b.tasks[uid].message()
    assert "BUY 65 x NSE:NIFTY26D0825600CE  LIMIT 120.60" in msg
    assert "SL-LMT  trigger 100.00  limit 99.50" in msg
    assert m.long_qty == 0 and m.state is PositionState.ENTRY_PENDING  # nothing assumed
    b.confirm_entry(uid, D("120.40"))
    asyncio.run(r.pump())
    assert m.long_qty == 65 and (uid, "stop_requested") in seen
    assert b.tasks[uid].self_reported


def test_missing_stop_escalates_every_minute_until_confirmed() -> None:
    c, b, r, m, _, alerts = setup()
    uid = start(c, b, r, m)
    b.confirm_entry(uid, D("120.40"))
    asyncio.run(r.pump())
    c.add(100)
    b.tick()
    assert alerts == []
    c.add(30)  # 130 s after entry
    b.tick()
    c.add(30)
    b.tick()  # only 30 s since the last alert: no repeat yet
    c.add(31)
    b.tick()
    crit = [a for a in alerts if a.level == "critical"]
    assert len(crit) == 2 and "NO PROTECTIVE STOP" in crit[0].message
    b.confirm_stop(uid)
    asyncio.run(r.pump())
    assert m.state is PositionState.OPEN_PROTECTED
    c.add(120)
    b.tick()
    assert len([a for a in alerts if a.level == "critical"]) == 2  # stops once confirmed


def test_engine_exit_request_then_trader_reports_exit() -> None:
    c, b, r, m, seen, _ = setup()
    uid = start(c, b, r, m)
    b.confirm_entry(uid, D("120.40"))
    asyncio.run(r.pump())
    b.confirm_stop(uid)
    asyncio.run(r.pump())
    asyncio.run(r.execute("P1", m.request_exit(c(), "target")))
    assert (uid, "exit_requested") in seen and m.long_qty == 65
    b.confirm_exit(uid, D("131.00"))
    asyncio.run(r.pump())
    assert m.state is PositionState.CLOSED and m.long_qty == 0


def test_trader_exits_on_their_own_without_a_new_stop() -> None:
    c, b, r, m, seen, alerts = setup()
    uid = start(c, b, r, m)
    b.confirm_entry(uid, D("120.40"))
    asyncio.run(r.pump())
    b.confirm_stop(uid)
    asyncio.run(r.pump())
    b.confirm_exit(uid, D("115.00"))
    asyncio.run(r.pump())
    assert m.state is PositionState.CLOSED
    assert [e for _, e in seen].count("stop_requested") == 1  # no second stop requested
    assert not [a for a in alerts if "re-protecting" in a.message]


def test_skip_and_expiry() -> None:
    c, b, r, m, seen, _ = setup()
    uid = start(c, b, r, m)
    b.skip(uid)
    asyncio.run(r.pump())
    assert m.state is PositionState.CLOSED and (uid, "skipped") in seen
    c2, b2, r2, m2, seen2, _ = setup()
    uid2 = start(c2, b2, r2, m2)
    c2.add(301)  # task expiry (no re-pricing in SEMI)
    asyncio.run(r2.tick())
    assert m2.state is PositionState.CLOSED and (uid2, "expired") in seen2
