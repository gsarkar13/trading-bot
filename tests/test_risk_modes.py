"""Risk controls, mode resolution, arming, single instance, strategies table."""

from __future__ import annotations

import datetime as dt
import itertools
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

import app.strategies.plugins  # noqa: F401 - registers the implemented plugins
from app.adapters.base import BrokerOrder, BrokerOrderStatus
from app.config.loader import load_config
from app.core.timeutil import IST
from app.core.types import GateStatus, InstanceRole, Mode, Product, Side
from app.db.models import EventLog, StrategyHistory
from app.db.seed import sync_strategies_table
from app.engine.arming import Arming
from app.engine.instance import AlreadyRunning, InstanceLock, foreign_orders
from app.engine.modes import ModeInputs, live_loss_action, resolve_mode
from app.engine.risk import (
    Breakers,
    DataHealth,
    data_block_reasons,
    entry_block_reason,
    must_square_off,
)
from app.engine.sizing import size_lots
from app.engine.strategy_table import ChangeRefused, StrategyTable

CFG = load_config()


def ist(h: int, m: int, d: dt.date = dt.date(2026, 12, 2)) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(h, m), IST).astimezone(dt.UTC)


# ---------------------------------------------------------------- time guards
@pytest.mark.parametrize(
    ("h", "m", "blocked"),
    [
        (9, 14, True),
        (9, 15, True),
        (9, 29, True),
        (9, 30, False),
        (14, 45, False),
        (14, 46, True),
        (15, 31, True),
    ],
)
def test_time_guards(h: int, m: int, blocked: bool) -> None:
    assert (entry_block_reason(ist(h, m), CFG.time_guards) is not None) is blocked


def test_square_off_spares_delivery() -> None:
    assert must_square_off(ist(15, 10), CFG.time_guards, Product.MIS)
    assert not must_square_off(ist(15, 9), CFG.time_guards, Product.MIS)
    assert not must_square_off(ist(15, 20), CFG.time_guards, Product.CNC)


# ---------------------------------------------------------------- data gate
def test_every_bad_input_blocks_entries() -> None:
    now = ist(10, 0)
    good = DataHealth(now, now, now, True, False, True, True, 20.0, True)
    assert data_block_reasons(good, CFG.freshness, 500) == []
    cases = {
        "stale ticks": {"last_tick_at": now - dt.timedelta(seconds=30)},
        "stale option chain": {"chain_at": None},
        "gap in candles": {"candle_gap": True},
        "instrument not mapped": {"instruments_mapped": False},
        "token expired": {"token_valid": False},
        "clock drift unknown or too large": {"clock_drift_ms": 900.0},
        "indicators warming up": {"warmed_up": False},
    }
    for reason, change in cases.items():
        h = DataHealth(**{**good.__dict__, **change})
        assert data_block_reasons(h, CFG.freshness, 500) == [reason]


# ---------------------------------------------------------------- breakers
def test_daily_loss_counts_unrealized_and_survives_restart(migrated_engine: Engine) -> None:
    b = Breakers(migrated_engine, CFG.risk, "PAPER", "PC1")
    now = ist(11, 0)
    b.record_close(now, Decimal("-900"))
    assert not b.check(now).tripped
    st = b.check(now, unrealized=Decimal("-700"))  # -1,600 <= -1,500 (3% of 50k)
    assert st.tripped and "day loss" in st.reasons[0]
    restarted = Breakers(migrated_engine, CFG.risk, "PAPER", "PC1")
    assert restarted.check(ist(11, 5)).tripped  # stays tripped after a restart
    assert not restarted.check(ist(10, 0, dt.date(2026, 12, 3))).tripped  # new day
    with Session(migrated_engine) as s:
        assert s.scalar(select(func.count(EventLog.id)).where(EventLog.category == "RISK"))


def test_trades_per_day_and_streak_across_days(migrated_engine: Engine) -> None:
    b = Breakers(migrated_engine, CFG.risk, "PAPER", "PC1")
    for i in range(3):
        b.record_entry(ist(10, i))
    assert "max 3 trades/day" in b.check(ist(10, 5)).reasons
    d2, d3 = dt.date(2026, 12, 3), dt.date(2026, 12, 4)
    b.record_close(ist(10, 0, d2), Decimal("-10"))
    b.record_close(ist(11, 0, d2), Decimal("-10"))
    b.record_close(ist(10, 0, d3), Decimal("-10"))  # third loss, next day (Q7: spans days)
    st = b.check(ist(10, 1, d3))
    assert st.tripped and "3 consecutive losses" in st.reasons


def test_weekly_limit(migrated_engine: Engine) -> None:
    b = Breakers(migrated_engine, CFG.risk, "REAL", "PC1")
    for d in (dt.date(2026, 11, 30), dt.date(2026, 12, 1), dt.date(2026, 12, 2)):
        b.record_close(ist(11, 0, d), Decimal("-1400"))  # 4,200 >= 8% of 50k = 4,000
    assert any("week loss" in r for r in b.check(ist(11, 1, dt.date(2026, 12, 2))).reasons)


def test_manual_reset_needs_the_phrase_and_is_logged(migrated_engine: Engine) -> None:
    b = Breakers(migrated_engine, CFG.risk, "PAPER", "PC1")
    now = ist(11, 0)
    b.record_close(now, Decimal("-2000"))
    assert b.check(now).tripped
    assert not b.reset(now, "reset", "dashboard")
    assert b.reset(now, "RESET BREAKERS 2026-12-02", "dashboard")
    with Session(migrated_engine) as s:
        assert s.scalar(select(EventLog.id).where(EventLog.message.like("%RULE OVERRIDE%")))


# ---------------------------------------------------------------- sizing
def test_sizing_matches_phase0_and_never_raises_risk() -> None:
    kw = {
        "capital": 50000,
        "max_premium_pct": 40,
        "max_lots": 1,
        "backstop_multiple": 1.5,
        "max_backstop_loss_multiple": 1.5,
    }
    ok = size_lots(risk_inr=1000, stop_dist=12, premium=120, lot=65, **kw)  # type: ignore[arg-type]
    assert (ok.lots, ok.skip_reason) == (1, None)
    assert size_lots(risk_inr=1000, stop_dist=20, premium=120, lot=65, **kw).skip_reason == (  # type: ignore[arg-type]
        "SKIPPED_SIZE_RISK"
    )
    bn = size_lots(risk_inr=1000, stop_dist=10, premium=1400, lot=30, **kw)  # type: ignore[arg-type]
    assert bn.skip_reason == "SKIPPED_SIZE_PREMIUM_CAP"  # D50: BANKNIFTY ATM at Rs 50k
    frz = size_lots(
        risk_inr=9000, stop_dist=1, premium=10, lot=65, freeze_qty=64, **{**kw, "max_lots": 5}
    )  # type: ignore[arg-type]
    assert frz.skip_reason == "SKIPPED_SIZE_FREEZE"


# ---------------------------------------------------------------- modes (inv 10, 14)
def test_mode_resolution_exhaustively() -> None:
    combos = itertools.product(
        list(Mode),
        list(Mode),
        [True, False],
        list(GateStatus),
        [True, False],
        [True, False],
        list(InstanceRole),
        [True, False],
        [True, False],
    )
    n = 0
    for req, ceil, semi, gate, armed, data_ok, role, ip_ok, exec_ok in combos:
        i = ModeInputs(req, ceil, semi, gate, armed, data_ok, role, ip_ok, exec_ok)
        r = resolve_mode(i)
        n += 1
        assert r.mode.rank <= min(req.rank, ceil.rank)  # can only lower
        if r.mode.is_real_money:
            assert gate in (GateStatus.PASSED, GateStatus.OVERRIDDEN) and armed and data_ok
        if r.mode is Mode.LIVE:
            assert role is InstanceRole.EXECUTION and ip_ok and exec_ok
        if r.mode is Mode.SEMI:
            assert semi
        assert (r.mode is not req) == bool(r.reasons)  # every lowering says why
    assert n == 3 * 3 * 2 * 4 * 2 * 2 * 2 * 2 * 2  # 2,304 combinations


def test_restart_is_paper_until_armed() -> None:
    i = ModeInputs(
        Mode.LIVE,
        Mode.LIVE,
        False,
        GateStatus.PASSED,
        False,
        True,
        InstanceRole.EXECUTION,
        True,
        True,
    )
    r = resolve_mode(i)
    assert r.mode is Mode.PAPER and "real money not armed today" in r.reasons


def test_options_never_fall_back_to_semi_mid_session() -> None:
    assert live_loss_action(segment_is_options=True, semi_fallback=True) == "no_new_entries"
    assert live_loss_action(segment_is_options=False, semi_fallback=True) == "semi_entries"
    assert live_loss_action(segment_is_options=False, semi_fallback=False) == "no_new_entries"


# ---------------------------------------------------------------- arming
def test_arming_two_step_today_and_this_process_only(migrated_engine: Engine) -> None:
    a = Arming(migrated_engine, "AWS1", "proc-1")
    now = ist(9, 0)
    code = a.request(now, "telegram")
    assert not a.confirm(now, "0000" if code != "0000" else "1111", "telegram", {})
    assert a.confirm(now + dt.timedelta(seconds=30), code, "telegram", {"host": "x"})
    assert a.is_armed(ist(10, 0))
    assert not a.is_armed(ist(15, 31))  # ends with the trading day
    assert not a.is_armed(ist(10, 0, dt.date(2026, 12, 3)))  # not tomorrow
    assert not Arming(migrated_engine, "AWS1", "proc-2").is_armed(ist(10, 0))  # restart
    a.disarm(ist(10, 1), "manual")
    assert not a.is_armed(ist(10, 2))


def test_arming_code_expires_and_three_wrong_codes_reset(migrated_engine: Engine) -> None:
    a = Arming(migrated_engine, "AWS1", "proc-1")
    code = a.request(ist(9, 0), "telegram")
    assert not a.confirm(ist(9, 3), code, "telegram", {})  # > 2 minutes
    code = a.request(ist(9, 5), "telegram")
    wrong = f"{(int(code) + 1) % 10000:04d}"
    for _ in range(3):
        assert not a.confirm(ist(9, 5), wrong, "telegram", {})
    assert not a.confirm(ist(9, 5), code, "telegram", {})  # must start over


# ---------------------------------------------------------------- instance (inv 16)
def test_second_engine_cannot_start(tmp_path: Path) -> None:
    with InstanceLock(tmp_path / "engine.lock"), pytest.raises(AlreadyRunning):
        InstanceLock(tmp_path / "engine.lock").acquire()
    InstanceLock(tmp_path / "engine.lock").acquire()  # free again afterwards


def test_foreign_instance_orders_detected() -> None:
    def o(tag: str | None) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id="1",
            client_tag=tag,
            key=None,
            side=Side.BUY,
            qty=65,
            filled_qty=0,
            avg_fill_price=None,
            limit_price=None,
            trigger_price=None,
            order_type="LMT",
            status=BrokerOrderStatus.PENDING,
        )

    book = [o("AWS1261202000001"), o("LAP1261202000004"), o(None), o("manual")]
    assert [x.client_tag for x in foreign_orders(book, "AWS1")] == ["LAP1261202000004"]


# ---------------------------------------------------------------- strategies table (inv 17)
def test_strategy_changes_blocked_while_exposed(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as s:
        sync_strategies_table(s, ist(8, 0))
        s.commit()
    exposed = {"pdh_pdl_breakout"}
    t = StrategyTable(migrated_engine, lambda sid: sid in exposed, "PC1")
    t.set_active("ema200_hammer_rvol_oi", True, ist(9, 0), "dashboard")
    with pytest.raises(ChangeRefused, match="placeholder"):
        t.set_active("nr7_breakout", True, ist(9, 0), "dashboard")
    with pytest.raises(ChangeRefused, match="open positions"):
        t.set_active("pdh_pdl_breakout", False, ist(9, 0), "telegram")
    with pytest.raises(ChangeRefused, match="mode can't change"):
        t.set_mode("pdh_pdl_breakout", Mode.LIVE, ist(9, 0), "telegram")
    t.move("pdh_pdl_breakout", True, ist(9, 1), "telegram")  # priority may change
    rows = {r.id: r.priority for r in t.rows()}
    assert rows["pdh_pdl_breakout"] == 1 and rows["ema200_hammer_rvol_oi"] == 2
    with Session(migrated_engine) as s:
        assert s.scalar(select(func.count(StrategyHistory.id))) >= 11 + 3
