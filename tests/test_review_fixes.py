"""Regression tests for the Phase 2 fresh-eyes review findings (each was reproduced
before its fix)."""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.adapters.base import InstrumentInfo, OrderRequest
from app.brokers.paper import Depth, PaperBroker, PaperConfig
from app.config.schema import ExecutionMechanics
from app.core.types import InstrumentKey, OptionType, OrderType, PositionState, Product, Side
from app.db.models import Order as OrderRow
from app.engine.oms import Alert, Cancel, Place, PositionMachine, Quote
from app.engine.router import OrderRouter
from app.engine.store import DbOrderStore, PositionMeta

D = Decimal
KEY = InstrumentKey.option("NIFTY", dt.date(2026, 12, 8), 25600, OptionType.CE)
INFO = {
    KEY: InstrumentInfo(
        key=KEY, broker_symbol="X", broker_token="1", lot_size=65, tick_size=D("0.05")
    )
}


class Clock:
    def __init__(self) -> None:
        self.t = dt.datetime(2026, 12, 2, 4, 30, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.t


class LostRejectedExit(PaperBroker):
    """The first plain SELL LMT is rejected by the broker, but the reply is lost."""

    raised = False

    async def place_order(self, req: OrderRequest):  # type: ignore[no-untyped-def]
        if req.side is Side.SELL and req.order_type is OrderType.LMT and not self.raised:
            self.raised = True
            self.cfg.reject_rate = 1.0
            self.place_sync(req)
            self.cfg.reject_rate = 0.0
            raise TimeoutError("read timeout")
        return self.place_sync(req)


def test_review1_lost_reply_on_rejected_exit_still_exits(migrated_engine: Engine) -> None:
    c = Clock()
    b = LostRejectedExit(INFO, c, PaperConfig(latency_s=0.0))
    b.supports_modify_to_exit = False  # cancel-then-exit path
    store = DbOrderStore(migrated_engine, c)
    alerts: list[Alert] = []
    r = OrderRouter(b, store, c, alerts.append)
    seq = itertools.count(1)
    m = PositionMachine(
        "P1",
        65,
        D("0.05"),
        65,
        D("100.00"),
        ExecutionMechanics(),
        lambda: f"PC1261202{next(seq):06d}",
    )
    store.open_position(
        m,
        KEY.canonical,
        PositionMeta("PC", "PAPER", "PAPER", "paper", "s", None, "MIS", D(1), None, "h"),
        c(),
    )
    r.track(m, KEY, Product.MIS)
    b.on_market(KEY, Depth(D("120.00"), D("120.50"), 650, 650, D("120.2"), c()))

    async def go() -> None:
        await r.execute("P1", m.start_entry(c(), Quote(D("120.00"), D("120.50"), None, c())))
        await r.execute("P1", m.request_exit(c(), "target"))  # must not raise
        for _ in range(6):
            c.t += dt.timedelta(seconds=3)
            b.advance()
            await r.tick()

    asyncio.run(go())
    assert m.state is PositionState.CLOSED and m.long_qty == 0 and b._long(KEY) == 0
    with Session(migrated_engine) as s:
        statuses = {o.client_tag: o.status for o in s.scalars(select(OrderRow))}
    assert "UNCERTAIN" not in statuses.values()  # the DB agrees with the machine


def test_review6_late_entry_fill_after_close_is_exited() -> None:
    seq = itertools.count(1)
    m = PositionMachine(
        "P2", 65, D("0.05"), 130, D("100.00"), ExecutionMechanics(), lambda: f"T{next(seq)}"
    )
    t0 = dt.datetime(2026, 12, 2, 4, 30, tzinfo=dt.UTC)
    [entry] = m.start_entry(t0, Quote(D("120.00"), D("120.50"), D("120.2"), t0))
    assert isinstance(entry, Place)
    m.on_ack(t0, entry.tag)
    prot = next(x for x in m.on_fill(t0, entry.tag, 65, D("120.5")) if isinstance(x, Place))
    m.on_ack(t0, prot.tag)
    m.request_exit(t0, "strategy stop")
    m.on_fill(t0, prot.tag, 65, D("119"))
    assert m.state is PositionState.CLOSED
    cmds = m.on_fill(t0, entry.tag, 130, D("120.5"))  # the cancelled rest filled anyway
    assert m.state is PositionState.EXITING and m.long_qty == 65
    sells = [x for x in cmds if isinstance(x, Place) and x.side is Side.SELL]
    assert sells and sells[0].qty == 65


def test_review7_refused_modify_cancels_and_replans() -> None:
    seq = itertools.count(1)
    m = PositionMachine(
        "P3", 65, D("0.05"), 65, D("100.00"), ExecutionMechanics(), lambda: f"T{next(seq)}"
    )
    t0 = dt.datetime(2026, 12, 2, 4, 30, tzinfo=dt.UTC)
    [entry] = m.start_entry(t0, Quote(D("120.00"), D("120.50"), D("120.2"), t0))
    assert isinstance(entry, Place)
    m.on_ack(t0, entry.tag)
    prot = next(x for x in m.on_fill(t0, entry.tag, 65, D("120.5")) if isinstance(x, Place))
    m.on_ack(t0, prot.tag)
    m.request_exit(t0, "target")  # stop -> exit via modify (in our record)
    cmds = m.on_modify_rejected(t0, prot.tag, "not allowed")
    assert any(isinstance(x, Cancel) and x.tag == prot.tag for x in cmds)
    after = m.on_cancelled(t0, prot.tag)  # broker confirms the cancel
    assert any(isinstance(x, Place) and x.side is Side.SELL for x in after)  # real exit sent


def test_review2_bot_token_never_reaches_a_log(tmp_path: Path) -> None:
    import logging

    from app.core.logsetup import setup_logging

    tok = "123456789:" + "A" * 35
    log_file = tmp_path / "w.log"
    setup_logging(log_file)
    logging.getLogger("httpx2").warning("HTTP Request: POST https://api.telegram.org/bot%s/x", tok)
    logging.getLogger("app").info("url https://api.telegram.org/bot" + tok + "/getUpdates")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_file.read_text()
    assert tok not in text and "[REDACTED]" in text
    assert logging.getLogger("httpx2").level == logging.WARNING


def test_review3_photo_from_owner_does_not_crash_the_bot() -> None:
    from app.telegram.bot import Bot
    from app.telegram.link import EngineLink

    sent: list[str] = []

    class Tg:
        async def send(self, chat_id: int, text: str, buttons: object = None) -> None:
            sent.append(text)

        async def answer(self, callback_id: str, text: str = "") -> None:
            pass

    async def flat() -> str:
        return ""

    bot = Bot(42, Tg(), EngineLink(1, Path("/nonexistent")), Clock(), flat)
    asyncio.run(bot.handle({"update_id": 1, "message": {"chat": {"id": 42}, "photo": [{}]}}))
    assert sent and "text commands" in sent[0]


def test_windows_pid_file_is_separate_from_the_lock(tmp_path: Path) -> None:
    import os

    from app.engine.instance import InstanceLock

    lk = InstanceLock(tmp_path / "engine-LAP1.lock")
    lk.acquire()
    try:
        assert (tmp_path / "engine-LAP1.pid").read_text().strip() == str(os.getpid())
    finally:
        lk.release()


def test_review9_one_failing_timer_step_does_not_stop_the_others(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    from tests.engine_harness import Harness, activate, ist

    activate(migrated_engine)
    h = Harness(migrated_engine, tmp_path)
    beats: list[dt.datetime] = []

    async def boom() -> None:
        raise RuntimeError("bad book")

    h.engine._foreign_check = lambda now: boom()  # type: ignore[method-assign]
    h.engine._heartbeat = lambda now: beats.append(now)  # type: ignore[method-assign]
    h.at(ist(10, 0))
    asyncio.run(h.engine.on_timer())
    h.at(ist(10, 0, 1))
    asyncio.run(h.engine.on_timer())
    assert len(beats) == 2  # heartbeat still written
    crit = [a for a in h.alerts.recent if a.level == "critical"]
    assert len(crit) == 1 and "foreign-order scan" in crit[0].message  # throttled
