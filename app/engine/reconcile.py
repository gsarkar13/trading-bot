"""Restart reconciliation (invariant 7) and order tags that never repeat (invariant 5).

On start the broker is the truth. For each position our database thinks is open, and for
each broker position we know nothing about, a `PositionMachine` is rebuilt with
`PositionMachine.adopt`. A held position without a working stop is UNMANAGED: the engine
protects it or flattens it (`execution.unmanaged_action`) and raises a critical alert.
Orders our database thinks are working but the broker doesn't list never reached it.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.adapters.base import BrokerOrder, BrokerOrderStatus, BrokerPosition
from app.core.tags import make_tag, parse_tag
from app.core.timeutil import to_ist
from app.core.types import InstrumentKey, OrderStatus, OrderType, Side
from app.db.models import Order as OrderRow
from app.db.models import Position as PositionRow
from app.engine.instance import foreign_orders
from app.engine.oms import Order, Purpose

OPEN_STATES = ("SIGNALLED", "ENTRY_PENDING", "PARTIAL", "OPEN_PROTECTED", "EXITING", "UNMANAGED")

_STATUS = {
    BrokerOrderStatus.PENDING: OrderStatus.PENDING,
    BrokerOrderStatus.TRIGGER_PENDING: OrderStatus.TRIGGER_PENDING,
    BrokerOrderStatus.PARTIAL: OrderStatus.PARTIAL,
    BrokerOrderStatus.FILLED: OrderStatus.FILLED,
    BrokerOrderStatus.CANCELLED: OrderStatus.CANCELLED,
    BrokerOrderStatus.REJECTED: OrderStatus.REJECTED,
    BrokerOrderStatus.UNKNOWN: OrderStatus.UNCERTAIN,
}


class TagSource:
    """Today's next tag, continuing after the highest sequence already used today."""

    def __init__(self, instance_id: str, today: dt.date, used: Sequence[str] = ()) -> None:
        self.instance_id, self.today = instance_id, today
        start = 1
        for t in used:
            p = parse_tag(t)
            if p and p[0] == instance_id and p[1] == today:
                start = max(start, p[2] + 1)
        self._seq: Iterator[int] = itertools.count(start)

    def __call__(self) -> str:
        return make_tag(self.instance_id, self.today, next(self._seq))


def used_tags(engine: Engine, book: Sequence[BrokerOrder]) -> list[str]:
    with Session(engine) as s:
        tags = list(s.scalars(select(OrderRow.client_tag)))
    return tags + [o.client_tag for o in book if o.client_tag]


@dataclass
class Recovered:
    position_uid: str
    key: InstrumentKey
    orders: list[Order]
    broker_long: int
    broker_avg: Decimal | None
    backstop: Decimal | None
    known: bool  # False: a broker position our database has no record of
    oids: dict[str, str] = field(default_factory=dict)
    strategy_id: str | None = None
    book: str | None = None
    outside_exit_avg: Decimal | None = None  # our sells the DB never saw (offline flatten)


@dataclass
class Report:
    recovered: list[Recovered]
    foreign: list[BrokerOrder]
    orphan_orders: list[BrokerOrder]  # our tag, working at the broker, no open position
    notes: list[str]


def _from_row(r: OrderRow, b: BrokerOrder | None, now: dt.datetime) -> Order:
    if b is None:
        # never reached the broker (crash between store and send): not working
        status = (
            OrderStatus.REJECTED
            if r.status in ("NEW", "SENT", "UNCERTAIN")
            else (OrderStatus(r.status))
        )
        if status in (OrderStatus.PENDING, OrderStatus.TRIGGER_PENDING, OrderStatus.PARTIAL):
            status = OrderStatus.CANCELLED
        filled, avg = r.filled_qty, None
        qty, limit, trigger, otype = r.qty, r.limit_price, r.trigger_price, OrderType(r.order_type)
    else:
        status = _STATUS[b.status]
        filled, avg, qty = b.filled_qty, b.avg_fill_price, b.qty
        limit = b.limit_price or r.limit_price
        trigger = b.trigger_price
        otype = (
            OrderType(b.order_type)
            if b.order_type in ("LMT", "SL-LMT")
            else OrderType(r.order_type)
        )
    purpose: Purpose = r.purpose  # type: ignore[assignment]
    return Order(
        r.client_tag,
        purpose,
        Side(r.side),
        qty,
        otype,
        limit,
        trigger,
        status=status,
        filled=filled,
        avg_price=avg,
        created_at=r.created_at,
        last_change=now,
        first_limit=limit,
        sends=1,
    )


def reconcile(
    engine: Engine,
    instance_id: str,
    order_book: Sequence[BrokerOrder],
    positions: Sequence[BrokerPosition],
    now: dt.datetime,
    new_uid: Callable[[], str],
    book: str | None = None,
    mode: str | None = None,
) -> Report:
    """Pair the database's open positions with what the broker holds.

    `book` / `mode` pick which stored positions belong to the broker account being
    reconciled (each paper book is its own simulated account)."""
    by_tag = {o.client_tag: o for o in order_book if o.client_tag}
    held: dict[str, tuple[int, Decimal | None]] = {}
    for p in positions:
        if p.key is not None and p.net_qty:
            q, _ = held.get(p.key.canonical, (0, None))
            held[p.key.canonical] = (q + p.net_qty, p.avg_price)
    notes: list[str] = []
    out: list[Recovered] = []
    claimed: set[str] = set()
    with Session(engine) as s:
        rows = [
            pr
            for pr in s.scalars(
                select(PositionRow).where(
                    PositionRow.instance_id == instance_id, PositionRow.state.in_(OPEN_STATES)
                )
            )
            if (book is None or pr.book == book) and (mode is None or pr.mode == mode)
        ]
        per_row = []
        for pr in rows:
            orows = list(
                s.scalars(
                    select(OrderRow)
                    .where(OrderRow.position_uid == pr.position_uid)
                    .order_by(OrderRow.id)
                )
            )
            claimed |= {r.client_tag for r in orows}
            per_row.append((pr, orows))
    # Our own filled sells that no stored order explains (e.g. the standalone flatten):
    # they price an exit made while the engine was down.
    outside: dict[str, tuple[int, Decimal]] = {}
    for bo in order_book:
        tag_info = parse_tag(bo.client_tag) if bo.client_tag else None
        if (
            bo.client_tag
            and bo.client_tag not in claimed
            and tag_info
            and tag_info[0] == instance_id
            and bo.side is Side.SELL
            and bo.filled_qty
            and bo.avg_fill_price
            and bo.key is not None
        ):
            q0, v0 = outside.get(bo.key.canonical, (0, Decimal(0)))
            outside[bo.key.canonical] = (q0 + bo.filled_qty, v0 + bo.avg_fill_price * bo.filled_qty)
    for pr, orows in per_row:
        orders = [_from_row(r, by_tag.get(r.client_tag), now) for r in orows]
        own = sum(o.filled for o in orders if o.side is Side.BUY) - sum(
            o.filled for o in orders if o.side is Side.SELL
        )
        q, avg = held.get(pr.instrument_key, (0, None))
        allot = max(0, min(own, q))  # only what this position's own orders explain
        if q - allot:
            held[pr.instrument_key] = (q - allot, avg)
        else:
            held.pop(pr.instrument_key, None)
        oq, ov = outside.get(pr.instrument_key, (0, Decimal(0)))
        out.append(
            Recovered(
                pr.position_uid,
                InstrumentKey.from_canonical(pr.instrument_key),
                orders,
                allot,
                avg or pr.avg_entry_price,
                pr.backstop_sl_price,
                True,
                {
                    r.client_tag: by_tag[r.client_tag].broker_order_id
                    for r in orows
                    if r.client_tag in by_tag
                },
                pr.strategy_id,
                pr.book,
                (ov / oq).quantize(Decimal("0.01")) if oq else None,
            )
        )
    for canon, (q, avg) in held.items():
        if q < 0:
            notes.append(f"broker shows a SHORT position {canon} {q}: handle it manually")
            continue
        notes.append(f"broker holds {q} of {canon} with no record here: adopting it")
        out.append(
            Recovered(new_uid(), InstrumentKey.from_canonical(canon), [], q, avg, None, False)
        )
    working = (
        BrokerOrderStatus.PENDING,
        BrokerOrderStatus.TRIGGER_PENDING,
        BrokerOrderStatus.PARTIAL,
    )
    orphans = []
    for bo in order_book:
        tag = bo.client_tag
        parsed = parse_tag(tag) if tag else None
        if (
            tag
            and tag not in claimed
            and parsed
            and parsed[0] == instance_id
            and bo.status in working
        ):
            orphans.append(bo)
    for o in orphans:
        notes.append(f"working order {o.client_tag} belongs to no open position: cancelling")
    return Report(out, foreign_orders(order_book, instance_id), orphans, notes)


def trading_day(now: dt.datetime) -> dt.date:
    return to_ist(now).date()
