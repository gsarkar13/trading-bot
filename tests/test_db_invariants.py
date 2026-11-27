"""The database itself refuses states that break invariants 1 and 2 and the LMT-only rule."""

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.types import EXIT_PURPOSES, WORKING_ORDER_STATUSES, OrderStatus
from app.db.models import Order, Position

NOW = dt.datetime(2026, 11, 27, 4, 0, tzinfo=dt.UTC)
_seq = iter(range(1, 10**6))


def _position(s: Session, uid: str = "P1", qty_open: int = 65) -> None:
    s.add(
        Position(
            position_uid=uid,
            instance_id="AWS1",
            book="REAL",
            mode="LIVE",
            broker="flattrade",
            instrument_key="OPT|NIFTY|2026-12-01|24000|CE",
            product="MIS",
            state="OPEN_PROTECTED",
            qty_open=qty_open,
        )
    )
    s.flush()


def _order(purpose: str, status: str, order_type: str = "SL-LMT", uid: str = "P1") -> Order:
    return Order(
        client_tag=f"AWS1261127{next(_seq):06d}",
        instance_id="AWS1",
        position_uid=uid,
        book="REAL",
        mode="LIVE",
        broker="flattrade",
        purpose=purpose,
        instrument_key="OPT|NIFTY|2026-12-01|24000|CE",
        side="SELL",
        order_type=order_type,
        product="MIS",
        qty=65,
        limit_price=Decimal("99"),
        trigger_price=Decimal("100"),
        status=status,
        created_at=NOW,
        updated_at=NOW,
    )


def test_second_working_exit_order_rejected(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as s:
        _position(s)
        s.add(_order("PROTECT", "TRIGGER_PENDING"))
        s.flush()
        s.add(_order("EXIT", "NEW"))  # target hit: exit placed before SL cancelled -> refused
        with pytest.raises(IntegrityError, match="UNIQUE"):
            s.flush()


def test_exit_allowed_once_stop_is_cancelled(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as s:
        _position(s)
        stop = _order("PROTECT", "TRIGGER_PENDING")
        s.add(stop)
        s.flush()
        stop.status = "CANCELLED"
        s.flush()
        s.add(_order("EXIT", "NEW", order_type="LMT"))
        s.flush()  # no error


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    first=st.tuples(st.sampled_from(EXIT_PURPOSES), st.sampled_from(list(OrderStatus))),
    second=st.tuples(st.sampled_from(EXIT_PURPOSES), st.sampled_from(list(OrderStatus))),
)
def test_one_working_exit_property(
    migrated_engine: Engine, first: tuple[str, OrderStatus], second: tuple[str, OrderStatus]
) -> None:
    both_working = first[1] in WORKING_ORDER_STATUSES and second[1] in WORKING_ORDER_STATUSES
    with Session(migrated_engine) as s:
        uid = f"P{next(_seq)}"
        _position(s, uid)
        s.add(_order(first[0], first[1].value, uid=uid))
        s.flush()
        s.add(_order(second[0], second[1].value, uid=uid))
        if both_working:
            with pytest.raises(IntegrityError):
                s.flush()
        else:
            s.flush()
        s.rollback()


def test_entry_orders_do_not_count_as_exits(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as s:
        _position(s)
        s.add(_order("ENTRY", "PENDING", order_type="LMT"))
        s.add(_order("PROTECT", "TRIGGER_PENDING"))
        s.flush()


@pytest.mark.parametrize("order_type", ["MKT", "SL-MKT", "MARKET"])
def test_market_order_types_refused_by_db(migrated_engine: Engine, order_type: str) -> None:
    with Session(migrated_engine) as s:
        _position(s)
        s.add(_order("EXIT", "NEW", order_type=order_type))
        with pytest.raises(IntegrityError, match="ck_orders_limit_types_only"):
            s.flush()


def test_negative_open_quantity_refused(migrated_engine: Engine) -> None:
    with (
        Session(migrated_engine) as s,
        pytest.raises(IntegrityError, match="ck_positions_never_short"),
    ):
        _position(s, qty_open=-65)
