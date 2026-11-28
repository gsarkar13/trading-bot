"""Order state machine: scenario tests per flow, plus property tests that throw random
broker event sequences at it and check invariants 1, 2, 3 and 5 after every event."""

from __future__ import annotations

import datetime as dt
import itertools
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.config.schema import ExecutionMechanics
from app.core.types import EXIT_PURPOSES, OrderStatus, OrderType, PositionState, Side
from app.engine.oms import (
    Alert,
    Cancel,
    InvariantViolation,
    Lookup,
    Modify,
    Place,
    PositionMachine,
    Quote,
    check_invariants,
)

T0 = dt.datetime(2026, 12, 2, 4, 30, tzinfo=dt.UTC)
CFG = ExecutionMechanics()
D = Decimal


def t(s: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=s)


def q(bid: str, ask: str, ltp: str | None = None, at: float = 0) -> Quote:
    return Quote(D(bid), D(ask), D(ltp or bid), t(at))


def machine(qty: int = 65, modify_to_exit: bool = True) -> PositionMachine:
    seq = itertools.count(1)
    return PositionMachine(
        "P1",
        65,
        D("0.05"),
        qty,
        D("100.00"),
        CFG,
        lambda: f"PC1261202{next(seq):06d}",
        modify_to_exit=modify_to_exit,
    )


def only(cmds: list[Any], kind: type) -> list[Any]:
    return [c for c in cmds if isinstance(c, kind)]


def open_position(m: PositionMachine) -> tuple[str, str]:
    """Entry sent, acked, filled; protective stop sent and acked."""
    [entry] = m.start_entry(t(0), q("120.00", "120.50"))
    assert isinstance(entry, Place) and entry.limit == D("120.60")  # ask + 2 ticks
    m.on_ack(t(0.1), entry.tag)
    [prot] = m.on_fill(t(0.5), entry.tag, 65, D("120.55"))
    assert isinstance(prot, Place) and prot.purpose == "PROTECT"
    assert (prot.order_type, prot.side, prot.qty, prot.trigger) == (
        OrderType.SL_LMT,
        Side.SELL,
        65,
        D("100.00"),
    )
    assert prot.limit == D("99.50")  # 10 ticks under the trigger
    m.on_ack(t(0.7), prot.tag)
    assert m.state is PositionState.OPEN_PROTECTED
    return entry.tag, prot.tag


def test_happy_path_modify_resting_stop_into_exit() -> None:
    m = machine()
    _, prot = open_position(m)
    m.on_quote(t(60), q("130.00", "130.40", at=60))
    cmds = m.request_exit(t(60), "target")
    assert cmds == [Modify(prot, limit=D("129.90"), order_type=OrderType.LMT)]
    assert len(m.working(*EXIT_PURPOSES)) == 1  # still one exit-side order (invariant 2)
    m.on_fill(t(61), prot, 65, D("129.95"))
    assert m.state is PositionState.CLOSED and m.long_qty == 0


def test_cancel_then_exit_when_broker_cannot_modify() -> None:
    m = machine(modify_to_exit=False)
    _, prot = open_position(m)
    cmds = m.request_exit(t(60), "target")
    assert cmds == [Cancel(prot)]  # no exit yet: the stop is still resting
    check_invariants(m)
    [exit_] = m.on_cancelled(t(61), prot)
    assert isinstance(exit_, Place) and exit_.purpose == "EXIT" and exit_.qty == 65
    check_invariants(m)


def test_stop_filled_before_cancel_means_nothing_more() -> None:
    m = machine(modify_to_exit=False)
    _, prot = open_position(m)
    m.request_exit(t(60), "target")
    cmds = m.on_fill(t(60.2), prot, 65, D("99.60"))  # the SL filled first
    assert only(cmds, Place) == [] and m.state is PositionState.CLOSED
    assert m.on_cancelled(t(60.5), prot) == []  # late cancel reply changes nothing


def test_partial_fill_reprice_then_cancel_rest_and_size_stop() -> None:
    m = machine(qty=130)
    [entry] = m.start_entry(t(0), q("120.00", "120.50"))
    m.on_ack(t(0.1), entry.tag)
    [prot] = m.on_fill(t(0.5), entry.tag, 65, D("120.55"))
    assert prot.qty == 65  # protects only what has filled
    m.on_ack(t(0.6), prot.tag)
    m.on_quote(t(2), q("120.80", "121.00", at=2))
    assert m.on_timer(t(2.6)) == [Modify(entry.tag, limit=D("121.10"))]
    m.on_quote(t(5), q("124.00", "124.50", at=5))  # ran away beyond 2% slippage
    assert m.on_timer(t(5)) == [Cancel(entry.tag)]
    m.on_cancelled(t(5.2), entry.tag)
    assert m.long_qty == 65 and m.protect is not None and m.protect.qty == 65
    assert m.state is PositionState.OPEN_PROTECTED


def test_protective_stop_deadline_flattens() -> None:
    m = machine()
    [entry] = m.start_entry(t(0), q("120.00", "120.50"))
    m.on_ack(t(0.1), entry.tag)
    [prot] = m.on_fill(t(0.5), entry.tag, 65, D("120.55"))  # never acknowledged
    assert m.on_timer(t(3.0)) == []  # 2.5 s: not yet
    cmds = m.on_timer(t(3.6))
    assert only(cmds, Alert)[0].level == "critical" and Cancel(prot.tag) in cmds
    [flat] = only(m.on_cancelled(t(4), prot.tag), Place)
    assert flat.side is Side.SELL and flat.qty == 65


def test_rejected_stop_flattens() -> None:
    m = machine()
    [entry] = m.start_entry(t(0), q("120.00", "120.50"))
    [prot] = m.on_fill(t(0.5), entry.tag, 65, D("120.55"))
    cmds = m.on_reject(t(0.6), prot.tag, "price band")
    assert only(cmds, Alert)[0].level == "critical"
    [flat] = only(cmds, Place)
    assert flat.purpose == "FLATTEN" and flat.qty == 65


def test_gap_skipped_stop_is_chased_then_alerts() -> None:
    m = machine()
    open_position(m)
    m.on_quote(t(10), Quote(D("95.00"), D("95.50"), D("95.20"), t(10)))  # through 100 trigger
    assert m.on_timer(t(12)) == []
    [mod] = m.on_timer(t(13.1))
    assert isinstance(mod, Modify) and mod.limit is not None and mod.limit <= D("94.90")
    for i in range(CFG.gap_chase_max_attempts - 1):
        assert only(m.on_timer(t(17 + 4 * i)), Modify)
    alerts = only(m.on_timer(t(200)), Alert)
    assert alerts and alerts[0].level == "critical"


def test_uncertain_send_is_looked_up_before_any_retry() -> None:
    m = machine()
    [entry] = m.start_entry(t(0), q("120.00", "120.50"))
    cmds = m.on_send_failed(t(5), entry.tag, "timeout")
    assert Lookup(entry.tag) in cmds and only(cmds, Place) == []
    [again] = m.on_lookup(t(6), entry.tag, found=False)
    assert isinstance(again, Place) and again.tag == entry.tag  # same tag, once confirmed absent
    m2 = machine()
    [e2] = m2.start_entry(t(0), q("120.00", "120.50"))
    m2.on_send_failed(t(5), e2.tag, "timeout")
    cmds = m2.on_lookup(t(6), e2.tag, found=True, filled=65, avg_price=D("120.55"))
    assert only(cmds, Place)[0].purpose == "PROTECT"  # adopted the fill, now protecting


def test_entry_filling_during_exit_is_covered() -> None:
    m = machine(qty=130)
    [entry] = m.start_entry(t(0), q("120.00", "120.50"))
    [prot] = m.on_fill(t(0.5), entry.tag, 65, D("120.55"))
    m.on_ack(t(0.6), prot.tag)
    cmds = m.request_exit(t(1), "square-off")
    assert Cancel(entry.tag) in cmds
    late = m.on_fill(t(1.1), entry.tag, 130, D("120.60"))  # rest filled before the cancel
    assert late == [Modify(prot.tag, qty=130)] and m.long_qty == 130
    check_invariants(m)


def test_sell_guard_refuses_to_go_short() -> None:
    m = machine()
    with pytest.raises(InvariantViolation):
        m._guard_sell(1)  # nothing bought yet
    open_position(m)
    with pytest.raises(InvariantViolation):
        m._guard_sell(1)  # the protective stop already covers all 65


# ------------------------------------------------------------ property tests
EVENTS = st.lists(
    st.tuples(
        st.sampled_from(
            [
                "quote",
                "timer",
                "ack",
                "fill",
                "reject",
                "cancelled",
                "send_failed",
                "lookup_absent",
                "lookup_found",
                "exit",
            ]
        ),
        st.integers(0, 50),
        st.floats(0.0, 1.0),
        st.floats(0.1, 30.0),
    ),
    min_size=1,
    max_size=60,
)


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(events=EVENTS, qty_lots=st.integers(1, 3), modify_to_exit=st.booleans())
def test_random_broker_events_never_break_invariants(
    events: list[tuple[str, int, float, float]], qty_lots: int, modify_to_exit: bool
) -> None:
    m = machine(qty=65 * qty_lots, modify_to_exit=modify_to_exit)
    now = 0.0
    placed: dict[str, int] = {}
    absent_ok: set[str] = set()

    def run(cmds: list[Any]) -> None:
        for c in cmds:
            if isinstance(c, Place):
                if c.tag in placed and c.tag not in absent_ok:
                    raise AssertionError(f"re-sent {c.tag} without a not-found lookup")  # 5
                placed[c.tag] = placed.get(c.tag, 0) + 1
                absent_ok.discard(c.tag)
        check_invariants(m)

    run(m.start_entry(t(now), q("120.00", "120.50")))
    for kind, idx, frac, dtime in events:
        now += dtime
        tags = list(m.orders)
        o = m.orders[tags[idx % len(tags)]] if tags else None
        if kind == "quote":
            px = D("80") + D(int(frac * 800)) / 10
            run(m.on_quote(t(now), Quote(px - D("0.5"), px, px, t(now))))
        elif kind == "timer":
            run(m.on_timer(t(now)))
            if (
                m.first_fill_at is not None
                and m.long_qty > 0
                and (t(now) - m.first_fill_at).total_seconds() > CFG.protect_fill_within_s
                and m.state not in (PositionState.EXITING, PositionState.CLOSED)
            ):
                p = m.protect  # invariant 3: protected, or already on the way out
                assert p is not None and p.status is OrderStatus.TRIGGER_PENDING
        elif kind == "exit":
            run(m.request_exit(t(now), "target"))
        elif o is None or not o.working:
            continue
        elif kind == "ack":
            run(m.on_ack(t(now), o.tag))
        elif kind == "fill":
            cum = o.filled + max(1, int(frac * o.remaining))
            run(m.on_fill(t(now), o.tag, min(cum, o.qty), o.limit))
        elif kind == "reject":
            run(m.on_reject(t(now), o.tag, "random"))
        elif kind == "cancelled":
            run(m.on_cancelled(t(now), o.tag))
        elif kind == "send_failed":
            run(m.on_send_failed(t(now), o.tag, "timeout"))
        elif kind == "lookup_absent":
            if o.status is OrderStatus.UNCERTAIN and o.filled == 0:
                absent_ok.add(o.tag)
                run(m.on_lookup(t(now), o.tag, found=False))
        elif kind == "lookup_found":
            run(m.on_lookup(t(now), o.tag, found=True))
        assert m.long_qty >= 0
    assert all(tr.reason for tr in m.transitions)  # every transition has a reason
