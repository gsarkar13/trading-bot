"""Contract choice (9.1) and the database order store + ledger (inv 5, D18)."""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.adapters.base import InstrumentInfo
from app.adapters.fyers.symbols import SymbolMap, parse_master
from app.brokers.paper import Depth, PaperBroker, PaperConfig
from app.config.loader import REPO_ROOT
from app.config.schema import ContractSelection, ExecutionMechanics
from app.core.types import InstrumentKey, OptionType, PositionState, Product
from app.db.models import Fill, Order, OrderEvent, Position, Trade
from app.engine.contracts import backstop_trigger, choose_contract, liquidity_block
from app.engine.oms import PositionMachine, Quote
from app.engine.router import OrderRouter
from app.engine.store import DbOrderStore, PositionMeta
from app.research.costs import ChargesTable

D = Decimal
FIX = Path(__file__).parent / "fixtures" / "fyers"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


@pytest.fixture(scope="module")
def smap() -> SymbolMap:
    return SymbolMap(parse_master((FIX / "NSE_FO_sample.csv").read_text()))


def at(d: dt.date, h: int = 10) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(h), IST)


# ------------------------------------------------------------ contract choice
def test_nifty_nearest_weekly_atm(smap: SymbolMap) -> None:
    d = dt.date(2026, 11, 27)
    c = choose_contract(smap, "NIFTY", "LONG_CE", 24026.0, at(d), d, ContractSelection())
    assert c.reason == "ok" and c.key is not None
    assert (c.key.expiry, c.key.strike, c.key.option_type) == (
        dt.date(2026, 12, 1),
        D(24050),
        OptionType.CE,
    )


def test_nifty_expiry_day_uses_next_weekly_or_skips(smap: SymbolMap) -> None:
    d = dt.date(2026, 12, 1)
    c = choose_contract(smap, "NIFTY", "LONG_PE", 24000.0, at(d), d, ContractSelection())
    assert c.key is not None and c.key.expiry == dt.date(2026, 12, 8)
    skip = ContractSelection(nifty_expiry_day="skip")
    c2 = choose_contract(smap, "NIFTY", "LONG_PE", 24000.0, at(d), d, skip)
    assert c2.key is None and "expiry day" in c2.reason


def test_itm_one_strike_each_side(smap: SymbolMap) -> None:
    d = dt.date(2026, 11, 27)
    cfg = ContractSelection(strike_mode="ITM_1")
    ce = choose_contract(smap, "NIFTY", "LONG_CE", 24000.0, at(d), d, cfg)
    pe = choose_contract(smap, "NIFTY", "LONG_PE", 24000.0, at(d), d, cfg)
    assert ce.key is not None and ce.key.strike == D(23950)
    assert pe.key is not None and pe.key.strike == D(24050)


def test_banknifty_uses_monthly(smap: SymbolMap) -> None:
    d = dt.date(2026, 11, 27)
    c = choose_contract(smap, "BANKNIFTY", "LONG_CE", 51000.0, at(d), d, ContractSelection())
    assert c.key is not None and c.key.expiry is not None and c.key.expiry.day >= 22


def test_liquidity_spread_and_depth() -> None:
    cfg = ContractSelection()
    now = at(dt.date(2026, 12, 2))
    assert liquidity_block(None, 65, cfg) is not None
    assert liquidity_block(Depth(D(100), D(100.5), 650, 650, D(100), now), 65, cfg) is None
    assert "spread" in (
        liquidity_block(Depth(D(100), D(103), 650, 650, D(100), now), 65, cfg) or ""
    )
    assert "depth" in (
        liquidity_block(Depth(D(100), D(100.5), 650, 64, D(100), now), 65, cfg) or ""
    )


def test_backstop_is_on_the_tick_grid_and_below_entry() -> None:
    b = backstop_trigger(D("120.50"), 14.3, D("0.05"), "delta", 1.5, 35)
    assert b == D("99.05") and b % D("0.05") == 0
    assert backstop_trigger(D("120.00"), 0, D("0.05"), "pct", 1.5, 35) == D("78.00")
    assert backstop_trigger(D("5.00"), 100, D("0.05"), "delta", 1.5, 35) == D("0.05")


# ------------------------------------------------------------ DB store + ledger
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


def test_round_trip_is_stored_and_ledgered(migrated_engine: Engine) -> None:
    t = [dt.datetime(2026, 12, 2, 4, 30, tzinfo=dt.UTC)]

    def now() -> dt.datetime:
        return t[0]

    b = PaperBroker(INFO, now, PaperConfig(latency_s=0.0))
    store = DbOrderStore(migrated_engine, now)
    r = OrderRouter(b, store, now, lambda a: None)
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
    meta = PositionMeta(
        "PC",
        "PAPER:test",
        "PAPER",
        "paper",
        "pdh_pdl_breakout",
        None,
        "MIS",
        D("24000"),
        D("24100"),
        "abc",
    )
    store.open_position(m, KEY.canonical, meta, now())
    r.track(m, KEY, Product.MIS)
    b.on_market(KEY, Depth(D("120.00"), D("120.50"), 650, 650, D("120.2"), now()))

    async def go() -> None:
        await r.execute("P1", m.start_entry(now(), Quote(D("120.00"), D("120.50"), None, now())))
        t[0] += dt.timedelta(minutes=5)
        b.on_market(KEY, Depth(D("131.00"), D("131.40"), 650, 650, D("131.2"), now()))
        await r.pump()
        m.on_quote(now(), Quote(D("131.00"), D("131.40"), None, now()))
        await r.execute("P1", m.request_exit(now(), "target"))

    asyncio.run(go())
    assert m.state is PositionState.CLOSED
    with Session(migrated_engine) as s:
        orders = list(s.scalars(select(Order)))
        assert {o.purpose for o in orders} >= {"ENTRY"}
        assert all(o.order_type in ("LMT", "SL-LMT") for o in orders)
        first = s.scalars(select(OrderEvent).order_by(OrderEvent.id)).first()
        assert first is not None and first.to_status == "NEW"  # stored before sent (inv 5)
        fills = list(s.scalars(select(Fill)))
        assert sum(f.qty for f in fills) == 130
        p = s.scalar(select(Position))
        assert p is not None and p.state == "CLOSED" and p.qty_open == 0
        assert p.avg_entry_price == D("120.50") and p.closed_at is not None

    trade = store.record_trade(m, ChargesTable(REPO_ROOT / "config" / "charges.yaml"), "paper", [])
    assert trade is not None
    assert trade.gross_pnl == (D("131.00") - D("120.50")) * 65
    assert D(0) < trade.charges_total < D(100)
    assert trade.net_pnl == trade.gross_pnl - trade.charges_total
    assert trade.compliance_score == 100
    with Session(migrated_engine) as s:
        assert s.scalar(select(Trade.net_pnl)) == trade.net_pnl
