"""Engine end to end on paper: signal -> checks -> order -> exit -> ledger (Phase 2)."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.types import PositionState
from app.db.models import RiskState, SignalRow, Trade
from tests.engine_harness import OPT, Harness, activate, ist


def outcomes(db: Engine) -> list[tuple[str, dict[str, object]]]:
    with Session(db) as s:
        return [
            (r.outcome, r.checklist) for r in s.scalars(select(SignalRow).order_by(SignalRow.id))
        ]


def trades(db: Engine) -> list[Trade]:
    with Session(db) as s:
        return list(s.scalars(select(Trade)))


def test_signal_to_closed_trade_in_shadow_and_portfolio_books(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    activate(migrated_engine)
    h = Harness(migrated_engine, tmp_path)
    h.strategy.script[ist(10, 0).astimezone(dt.UTC)] = ("LONG_CE", 24000, 23985, 24030)

    async def go() -> None:
        await h.minute(10, 0, 24000)
        [(out, chk)] = outcomes(migrated_engine)
        assert out == "OPENED PAPER PAPER", out
        assert chk["shadow"] == "OPENED" and chk["lots"] == 1
        assert chk["contract"] == OPT.canonical
        ps = list(h.engine.positions.values())
        assert {p.book for p in ps} == {"SHADOW:pdh_pdl_breakout", "PAPER"}
        assert all(p.machine.state is PositionState.OPEN_PROTECTED for p in ps)
        await h.option("131.00", "131.40")
        await h.index(24031, ist(10, 1, 10))  # target on the index
        await h.engine.on_timer()

    asyncio.run(go())
    assert not h.engine.positions
    ts = trades(migrated_engine)
    assert len(ts) == 2 and {t.book for t in ts} == {"SHADOW:pdh_pdl_breakout", "PAPER"}
    for t in ts:
        assert t.entry_price == 120.5 and t.exit_price == 131.0 and t.qty == 65
        assert t.net_pnl < t.gross_pnl
    assert len(h.strategy.opened) == 1
    with Session(migrated_engine) as s:
        day = s.scalar(
            select(RiskState).where(RiskState.book == "PAPER", RiskState.period == "day")
        )
        assert day is not None and day.trades == 1 and day.realized_pnl == ts[0].net_pnl


def test_entries_blocked_by_time_guard_research_only_and_stale_data(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    activate(migrated_engine)
    h = Harness(migrated_engine, tmp_path)
    for t in (ist(9, 20), ist(10, 5), ist(10, 10)):
        h.strategy.script[t.astimezone(dt.UTC)] = ("LONG_CE", 24000, 23985, 24030)

    async def go() -> None:
        h.at(ist(9, 19))
        await h.minute(9, 20, 24000)
        await h.minute(10, 5, 52000, u="BANKNIFTY")
        h.strategy.script[ist(10, 5).astimezone(dt.UTC)] = ("LONG_CE", 24000, 23985, 24030)
        await h.index(24000, ist(10, 10, 0))
        await h.index(24000, ist(10, 10, 50))
        h.at(ist(10, 11, 9))  # closed late by the timer: last tick 19 s old
        await h.engine.on_timer()

    asyncio.run(go())
    outs = [o for o, _ in outcomes(migrated_engine)]
    assert "no entries 09:15-09:30" in outs[0]
    assert "research-only" in outs[1]
    assert "stale ticks" in outs[2]
    assert not h.engine.positions


def test_flatten_all_exits_and_stops_new_entries(migrated_engine: Engine, tmp_path: Path) -> None:
    activate(migrated_engine)
    h = Harness(migrated_engine, tmp_path)
    h.strategy.script[ist(10, 0).astimezone(dt.UTC)] = ("LONG_CE", 24000, 23985, 24030)
    h.strategy.script[ist(10, 1).astimezone(dt.UTC)] = ("LONG_CE", 24000, 23985, 24030)

    async def go() -> None:
        await h.minute(10, 0, 24000)
        assert len(h.engine.positions) == 2
        await h.option("119.00", "119.40")
        n = await h.engine.flatten_all("test", "dashboard")
        assert n == 2
        await h.engine.on_timer()
        assert not h.engine.positions
        await h.minute(10, 1, 24000)

    asyncio.run(go())
    out, chk = outcomes(migrated_engine)[-1]
    assert out.startswith("BLOCKED: entries halted"), out
    assert chk["shadow"] == "OPENED"  # the virtual shadow book keeps its own record
    assert len(trades(migrated_engine)) == 2


def test_square_off_at_1510(migrated_engine: Engine, tmp_path: Path) -> None:
    activate(migrated_engine)
    h = Harness(migrated_engine, tmp_path)
    h.strategy.script[ist(14, 40).astimezone(dt.UTC)] = ("LONG_CE", 24000, 23985, 24100)

    async def go() -> None:
        await h.minute(14, 40, 24000)
        assert len(h.engine.positions) == 2
        await h.option("121.00", "121.40")
        h.at(ist(15, 10, 0))
        await h.engine.on_timer()
        assert not h.engine.positions

    asyncio.run(go())
    assert len(trades(migrated_engine)) == 2


def test_lower_priority_is_superseded_but_its_shadow_book_trades(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    activate(migrated_engine)
    h = Harness(migrated_engine, tmp_path)
    h.strategy.script[ist(10, 0).astimezone(dt.UTC)] = ("LONG_CE", 24000, 23985, 24030)
    h.strategy.script[ist(10, 1).astimezone(dt.UTC)] = ("LONG_CE", 24000, 23985, 24030)

    async def go() -> None:
        await h.minute(10, 0, 24000)
        await h.minute(10, 1, 24000)

    asyncio.run(go())
    outs = outcomes(migrated_engine)
    assert outs[1][0] == "BLOCKED: position open"
    assert outs[1][1]["shadow"] == "POSITION_OPEN"
