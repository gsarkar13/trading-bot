"""Paper broker fill model (section 16.2) and the router driving the state machine on it."""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
from decimal import Decimal

import pytest

from app.adapters.base import (
    BrokerEvent,
    BrokerOrderStatus,
    InstrumentInfo,
    OrderRequest,
    PlaceResult,
)
from app.brokers.paper import Depth, PaperBroker, PaperConfig
from app.config.schema import ExecutionMechanics
from app.core.types import InstrumentKey, OptionType, OrderType, PositionState, Product, Side
from app.engine.oms import Alert, PositionMachine, Quote
from app.engine.router import MemoryStore, OrderRouter

D = Decimal
KEY = InstrumentKey.option("NIFTY", dt.date(2026, 12, 8), 25600, OptionType.CE)
INFO = {
    KEY: InstrumentInfo(
        key=KEY,
        broker_symbol="NSE:NIFTY26D0825600CE",
        broker_token="1",
        lot_size=65,
        tick_size=D("0.05"),
    )
}
T0 = dt.datetime(2026, 12, 2, 4, 30, tzinfo=dt.UTC)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> dt.datetime:
        return self.t

    def add(self, s: float) -> None:
        self.t += dt.timedelta(seconds=s)


def depth(
    bid: str,
    ask: str,
    bq: int = 650,
    aq: int = 650,
    ltp: str | None = None,
    clock: Clock | None = None,
) -> Depth:
    return Depth(D(bid), D(ask), bq, aq, D(ltp or bid), clock() if clock else T0)


def req(
    tag: str,
    side: Side,
    qty: int,
    limit: str,
    otype: OrderType = OrderType.LMT,
    trigger: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        client_tag=tag,
        key=KEY,
        side=side,
        qty=qty,
        order_type=otype,
        limit_price=D(limit),
        trigger_price=D(trigger) if trigger else None,
        product=Product.MIS,
    )


def broker(
    clock: Clock, latency: float = 0.0, **kw: object
) -> tuple[PaperBroker, list[BrokerEvent]]:
    b = PaperBroker(INFO, clock, PaperConfig(latency_s=latency, **kw))  # type: ignore[arg-type]
    events: list[BrokerEvent] = []
    b.set_event_sink(events.append)
    return b, events


# ------------------------------------------------------------ fill model
def test_buy_fills_only_at_or_through_the_ask_at_the_ask_price() -> None:
    c = Clock()
    b, ev = broker(c)
    b.on_market(KEY, depth("120.00", "120.50", clock=c))
    b.place_sync(req("PC1261202000001", Side.BUY, 65, "120.45"))
    assert [e.kind for e in ev] == ["ack"]  # below the ask: resting, no fill
    b.place_sync(req("PC1261202000002", Side.BUY, 65, "121.00"))
    fill = ev[-1]
    assert fill.kind == "fill" and fill.avg_price == D("120.50")  # at the ask, not the limit


def test_partial_fill_limited_by_displayed_depth() -> None:
    c = Clock()
    b, ev = broker(c)
    b.on_market(KEY, depth("120.00", "120.50", aq=65, clock=c))
    b.place_sync(req("PC1261202000001", Side.BUY, 195, "121.00"))
    assert ev[-1].cum_filled == 65
    b.on_market(KEY, depth("120.00", "120.50", aq=130, clock=c))
    assert ev[-1].cum_filled == 195 and ev[-1].kind == "fill"


def test_latency_delays_ack_and_fills() -> None:
    c = Clock()
    b, ev = broker(c, latency=0.5)
    b.on_market(KEY, depth("120.00", "120.50", clock=c))
    b.place_sync(req("PC1261202000001", Side.BUY, 65, "121.00"))
    assert ev == []
    c.add(0.6)
    b.advance()
    assert [e.kind for e in ev] == ["ack", "fill"]


def test_stop_limit_gapped_through_does_not_fill() -> None:
    c = Clock()
    b, ev = broker(c)
    b.on_market(KEY, depth("120.00", "120.50", clock=c))
    b.place_sync(req("PC1261202000001", Side.BUY, 65, "121.00"))
    b.place_sync(req("PC1261202000002", Side.SELL, 65, "99.50", OrderType.SL_LMT, "100.00"))
    b.on_market(KEY, depth("94.00", "94.50", ltp="94.20", clock=c))  # gap through 99.50
    assert ev[-1].kind == "ack"  # triggered, but no fill below its limit
    b.modify_sync(b.oid_for("PC1261202000002") or "", limit_price=D("93.50"))
    assert ev[-1].kind == "fill" and ev[-1].avg_price == D("94.00")


def test_stop_limit_triggers_and_fills_normally() -> None:
    c = Clock()
    b, ev = broker(c)
    b.on_market(KEY, depth("120.00", "120.50", clock=c))
    b.place_sync(req("PC1261202000001", Side.BUY, 65, "121.00"))
    b.place_sync(req("PC1261202000002", Side.SELL, 65, "99.50", OrderType.SL_LMT, "100.00"))
    b.on_market(KEY, depth("101.00", "101.50", ltp="101.20", clock=c))
    assert ev[-1].kind == "ack"  # not triggered yet
    b.on_market(KEY, depth("99.80", "100.20", ltp="99.90", clock=c))
    assert ev[-1].kind == "fill" and ev[-1].avg_price == D("99.80")


@pytest.mark.parametrize(
    ("r", "why"),
    [
        (req("PC1261202000001", Side.BUY, 60, "120.50"), "multiple of lot"),
        (req("PC1261202000001", Side.BUY, 65, "120.52"), "multiple of tick"),
        (req("PC1261202000001", Side.BUY, 65, "200.00"), "price band"),
        (req("PC1261202000001", Side.SELL, 65, "119.00"), "exceeds open long"),
    ],
)
def test_rejections(r: OrderRequest, why: str) -> None:
    c = Clock()
    b, ev = broker(c)
    b.on_market(KEY, depth("120.00", "120.50", clock=c))
    res = b.place_sync(r)
    assert res.outcome == "REJECTED" and why in (res.message or "")
    assert ev[-1].kind == "rejected"


def test_cancel_after_fill_is_refused_and_positions_funds() -> None:
    c = Clock()
    b, _ = broker(c)
    b.on_market(KEY, depth("120.00", "120.50", clock=c))
    b.place_sync(req("PC1261202000001", Side.BUY, 65, "121.00"))
    assert b.cancel_sync(b.oid_for("PC1261202000001") or "").outcome == "REJECTED"
    [pos] = asyncio.run(b.positions())
    assert pos.net_qty == 65 and pos.avg_price == D("120.50")
    f = asyncio.run(b.funds())
    assert f.available_cash == D("50000") - D("120.50") * 65


# ------------------------------------------------------------ router + machine + paper
def setup(
    latency: float = 0.0, **kw: object
) -> tuple[Clock, PaperBroker, OrderRouter, PositionMachine, list[Alert], MemoryStore]:
    c = Clock()
    b, _ = broker(c, latency, **kw)
    alerts: list[Alert] = []
    store = MemoryStore()
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
    r.track(m, KEY)
    b.on_market(KEY, depth("120.00", "120.50", clock=c))
    return c, b, r, m, alerts, store


def test_full_lifecycle_on_paper() -> None:
    c, b, r, m, alerts, store = setup()

    async def go() -> None:
        await r.execute("P1", m.start_entry(c(), Quote(D("120.00"), D("120.50"), D("120.2"), c())))
        assert m.state is PositionState.OPEN_PROTECTED and m.long_qty == 65
        prot = m.protect
        assert prot is not None and prot.order_type is OrderType.SL_LMT
        b.on_market(KEY, depth("131.00", "131.40", clock=c))
        await r.pump()
        m.on_quote(c(), Quote(D("131.00"), D("131.40"), D("131.2"), c()))
        await r.execute("P1", m.request_exit(c(), "target"))
        assert m.state is PositionState.CLOSED and m.long_qty == 0

    asyncio.run(go())
    statuses = [s for tag, s, _ in store.events]
    assert statuses.index("NEW") < statuses.index("SENT")  # stored before sent (inv 5)
    assert not [a for a in alerts if a.level == "critical"]
    [pos] = asyncio.run(b.positions()) or [None]  # type: ignore[list-item]
    assert pos is None


def test_gap_through_stop_is_chased_until_filled_on_paper() -> None:
    c, b, r, m, _alerts, _ = setup()

    async def go() -> None:
        await r.execute("P1", m.start_entry(c(), Quote(D("120.00"), D("120.50"), D("120.2"), c())))
        gap = depth("94.00", "94.50", ltp="94.20", clock=c)
        b.on_market(KEY, gap)
        m.on_quote(c(), Quote(gap.bid, gap.ask, gap.ltp, c()))
        await r.pump()
        assert m.long_qty == 65  # stop didn't fill through the gap
        for _ in range(3):
            c.add(3.5)
            await r.tick()
        assert m.long_qty == 0 and m.state is PositionState.CLOSED

    asyncio.run(go())


class TimeoutOncePaper(PaperBroker):
    """Accepts the order but the response is lost (chaos: order timeout)."""

    lost = False

    async def place_order(self, req: OrderRequest) -> PlaceResult:
        res = self.place_sync(req)
        if not self.lost:
            self.lost = True
            raise TimeoutError("response lost")
        return res


def test_lost_response_is_looked_up_not_resent() -> None:
    c = Clock()
    b = TimeoutOncePaper(INFO, c, PaperConfig(latency_s=0.0))
    alerts: list[Alert] = []
    r = OrderRouter(b, MemoryStore(), c, alerts.append)
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
    r.track(m, KEY)
    b.on_market(KEY, depth("120.00", "120.50", clock=c))
    asyncio.run(
        r.execute("P1", m.start_entry(c(), Quote(D("120.00"), D("120.50"), D("120.2"), c())))
    )
    book = asyncio.run(b.order_book())
    entries = [o for o in book if o.side is Side.BUY]
    assert len(entries) == 1  # found by tag; never sent twice
    assert entries[0].status is BrokerOrderStatus.FILLED and m.long_qty == 65
    assert any("uncertain" in a.message for a in alerts)


def test_compaction_keeps_open_positions_and_never_reuses_ids(tmp_path: object) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    c = Clock()
    f = tmp_path / "book.json"
    b = PaperBroker(INFO, c, PaperConfig(latency_s=0.0), state_path=f)
    b.on_market(KEY, depth("120.00", "120.50", clock=c))
    b.place_sync(req("PC1261202000001", Side.BUY, 65, "121.00"))
    b.place_sync(req("PC1261202000002", Side.SELL, 65, "119.00"))  # flat again
    b2 = PaperBroker(INFO, c, PaperConfig(latency_s=0.0), state_path=f)
    assert b2.compact() == 2 and asyncio.run(b2.order_book()) == []
    b2.on_market(KEY, depth("120.00", "120.50", clock=c))
    r = b2.place_sync(req("PC1261202000003", Side.BUY, 65, "121.00"))
    assert r.broker_order_id == "PAPER00000003"  # ids continue after compaction
    b3 = PaperBroker(INFO, c, PaperConfig(latency_s=0.0), state_path=f)
    assert b3.compact() == 0  # an open position is kept
    assert asyncio.run(b3.positions())[0].net_qty == 65
