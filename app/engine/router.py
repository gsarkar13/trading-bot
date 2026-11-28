"""Execution layer: runs PositionMachine commands on a broker and feeds broker events back.

  * Invariant 5: every order is saved (with its tag, status NEW) BEFORE it is sent. A
    send that times out or errors becomes UNCERTAIN, and the tag is looked up in the
    order book before anything is re-sent.
  * Invariant 12: all order operations together pass one rate limiter (default 4/s).
  * Invariants 1/2 are re-checked after every event; a violation is never sent on, and
    raises a critical alert instead.
The same router serves the paper, SEMI and live brokers.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from app.adapters.base import BaseExecutionBroker, BrokerEvent, OrderRequest
from app.core.ratelimit import RateLimiter
from app.core.types import InstrumentKey, OrderStatus, Product
from app.engine.oms import (
    Alert,
    Cancel,
    Command,
    InvariantViolation,
    Lookup,
    Modify,
    Place,
    PositionMachine,
    check_invariants,
)

log = logging.getLogger(__name__)


class OrderStore(Protocol):
    """Persistence the router needs. The engine's version writes the orders/order_events/
    fills tables; tests use an in-memory one."""

    def save_new(self, m: PositionMachine, cmd: Place, key: InstrumentKey) -> None: ...
    def save_status(
        self, tag: str, status: OrderStatus, filled: int, reason: str, purpose: str | None = None
    ) -> None: ...
    def save_fill(
        self, tag: str, cum_filled: int, price: Decimal | None, at: dt.datetime
    ) -> None: ...
    def sync_position(self, m: PositionMachine) -> None: ...


@dataclass
class MemoryStore:
    orders: dict[str, dict[str, object]] = field(default_factory=dict)
    events: list[tuple[str, str, str]] = field(default_factory=list)

    def save_new(self, m: PositionMachine, cmd: Place, key: InstrumentKey) -> None:
        if cmd.tag not in self.orders:
            self.orders[cmd.tag] = {
                "position": m.position_uid,
                "purpose": cmd.purpose,
                "status": "NEW",
                "key": key.canonical,
            }
        self.events.append((cmd.tag, "NEW", "stored before send"))

    def save_status(
        self, tag: str, status: OrderStatus, filled: int, reason: str, purpose: str | None = None
    ) -> None:
        if tag in self.orders:
            self.orders[tag]["status"] = status.value
            if purpose:
                self.orders[tag]["purpose"] = purpose
        self.events.append((tag, status.value, reason))

    def sync_position(self, m: PositionMachine) -> None:
        self.events.append((m.position_uid, m.state.value, f"long {m.long_qty}"))

    def save_fill(self, tag: str, cum_filled: int, price: Decimal | None, at: dt.datetime) -> None:
        self.events.append((tag, f"FILL {cum_filled}@{price}", at.isoformat()))


@dataclass
class _Tracked:
    machine: PositionMachine
    key: InstrumentKey
    product: Product


class OrderRouter:
    def __init__(
        self,
        broker: BaseExecutionBroker,
        store: OrderStore,
        now: Callable[[], dt.datetime],
        on_alert: Callable[[Alert], None],
        limiter: RateLimiter | None = None,
        send_timeout_s: float = 5.0,
    ) -> None:
        self.broker, self.store, self.now, self.on_alert = broker, store, now, on_alert
        self.limiter = limiter or RateLimiter(4, 200)
        self.send_timeout_s = send_timeout_s
        self.positions: dict[str, _Tracked] = {}
        self.owner: dict[str, str] = {}  # tag -> position uid
        self.oid: dict[str, str] = {}  # tag -> broker order id
        self._events: deque[BrokerEvent] = deque()
        self._deferred: dict[str, list[Command]] = {}
        self._settled: dict[str, dt.datetime] = {}
        self._replay: list[str] = []
        broker.set_event_sink(self._events.append)

    def track(self, m: PositionMachine, key: InstrumentKey, product: Product = Product.MIS) -> None:
        m.modify_to_exit = m.modify_to_exit and self.broker.supports_modify_to_exit
        self.positions[m.position_uid] = _Tracked(m, key, product)

    def adopt(
        self,
        m: PositionMachine,
        key: InstrumentKey,
        product: Product,
        oids: dict[str, str],
    ) -> None:
        """Track a machine rebuilt after a restart, with its orders' broker ids."""
        self.track(m, key, product)
        for tag in m.orders:
            self.owner[tag] = m.position_uid
        self.oid.update(oids)

    def untrack_closed(self, grace_s: float = 60.0) -> list[PositionMachine]:
        """Positions that have just finished, once each (for the ledger). They stay
        tracked for `grace_s` more seconds so a late broker event (an entry that filled
        after we cancelled it) still reaches its machine; then they are dropped."""
        now = self.now()
        newly: list[PositionMachine] = []
        for t in list(self.positions.values()):
            m = t.machine
            done = not m.working() and m.state.value in ("CLOSED", "REJECTED")
            if not done:
                self._settled.pop(m.position_uid, None)  # re-opened by a late fill
                continue
            at = self._settled.get(m.position_uid)
            if at is None:
                self._settled[m.position_uid] = now
                newly.append(m)
            elif (now - at).total_seconds() >= grace_s:
                del self.positions[m.position_uid]
                del self._settled[m.position_uid]
                for tag in m.orders:
                    self.owner.pop(tag, None)
                    self.oid.pop(tag, None)
                    self._deferred.pop(tag, None)
        return newly

    # ------------------------------------------------------------ commands -> broker
    def _known(self, tag: str, oid: str) -> None:
        self.oid[tag] = oid
        if tag in self._deferred:
            self._replay.append(tag)

    async def _run_replays(self) -> None:
        while self._replay:
            tag = self._replay.pop()
            cmds = self._deferred.pop(tag, [])
            puid = self.owner.get(tag)
            if puid is not None and puid in self.positions:
                for c in cmds:
                    await self._one(self.positions[puid], c)

    async def execute(self, puid: str, cmds: list[Command]) -> None:
        t = self.positions[puid]
        for c in cmds:
            await self._one(t, c)
        await self.pump()

    async def _one(self, t: _Tracked, c: Command) -> None:
        m = t.machine
        if isinstance(c, Alert):
            self.on_alert(c)
            return
        if isinstance(c, Place):
            self.owner[c.tag] = m.position_uid
            try:
                self.store.save_new(m, c, t.key)  # BEFORE sending (invariant 5)
            except Exception as e:  # e.g. "database is locked": never send unrecorded
                log.exception("could not store %s before sending", c.tag)
                self.on_alert(
                    Alert("critical", f"{c.tag}: not stored ({type(e).__name__}); not sent")
                )
                await self._feed(t, m.on_reject(self.now(), c.tag, "could not store"))
                return
            req = OrderRequest(
                client_tag=c.tag,
                key=t.key,
                side=c.side,
                qty=c.qty,
                order_type=c.order_type,
                limit_price=c.limit,
                trigger_price=c.trigger,
                product=t.product,
            )
            await self.limiter.acquire()
            try:
                res = await asyncio.wait_for(self.broker.place_order(req), self.send_timeout_s)
            except Exception as e:
                await self._feed(t, m.on_send_failed(self.now(), c.tag, type(e).__name__))
                return
            if res.broker_order_id:
                self._known(c.tag, res.broker_order_id)
            if res.outcome == "REJECTED":
                self.store.save_status(c.tag, OrderStatus.REJECTED, 0, res.message or "")
                await self._feed(t, m.on_reject(self.now(), c.tag, res.message or "rejected"))
            elif res.outcome == "UNCERTAIN":
                await self._feed(t, m.on_send_failed(self.now(), c.tag, res.message or "?"))
            else:
                self.store.save_status(c.tag, OrderStatus.SENT, 0, "accepted by broker")
            return
        if isinstance(c, Lookup):
            found = await self.broker.find_by_tag(c.tag)
            if found is not None:
                self._known(c.tag, found.broker_order_id)
            cmds = m.on_lookup(
                self.now(),
                c.tag,
                found is not None,
                filled=found.filled_qty if found else 0,
                avg_price=found.avg_fill_price if found else None,
                broker_status=found.status.value if found else "",
            )
            o = m.orders[c.tag]  # the machine's view AFTER the lookup, so the DB agrees
            self.store.save_status(
                c.tag, o.status, o.filled, f"lookup: {'found' if found else 'not at broker'}"
            )
            await self._feed(t, cmds)
            return
        oid = self.oid.get(c.tag)
        if oid is None:
            # replayed as soon as the broker id is known (ack, lookup)
            self._deferred.setdefault(c.tag, []).append(c)
            self.on_alert(
                Alert(
                    "warn",
                    f"{c.tag}: no broker id yet; {type(c).__name__} "
                    "deferred until the order is known",
                )
            )
            return
        await self.limiter.acquire()
        if isinstance(c, Modify):
            res = await self.broker.modify_order(
                oid,
                limit_price=c.limit,
                trigger_price=c.trigger,
                qty=c.qty,
                order_type=c.order_type,
            )
        else:
            assert isinstance(c, Cancel)
            res = await self.broker.cancel_order(oid)
        if res.outcome != "ACCEPTED":
            log.info("%s %s not accepted: %s", type(c).__name__, c.tag, res.message)
            if isinstance(c, Modify):
                await self._feed(
                    t, m.on_modify_rejected(self.now(), c.tag, res.message or "rejected")
                )
            # a refused Cancel: the order already filled or ended; its event settles it
        else:
            o = m.orders[c.tag]
            what = "modify requested" if isinstance(c, Modify) else "cancel requested"
            self.store.save_status(c.tag, o.status, o.filled, what, purpose=o.purpose)

    async def _feed(self, t: _Tracked, cmds: list[Command]) -> None:
        self._check(t)
        self.store.sync_position(t.machine)
        for c in cmds:
            await self._one(t, c)

    def _check(self, t: _Tracked) -> None:
        try:
            check_invariants(t.machine)
        except InvariantViolation as e:
            self.on_alert(Alert("critical", f"{t.machine.position_uid}: INVARIANT {e}"))
            raise

    # ------------------------------------------------------------ broker events -> machine
    async def pump(self) -> None:
        while self._events:
            ev = self._events.popleft()
            puid = self.owner.get(ev.client_tag)
            if puid is None or puid not in self.positions:
                if ev.kind == "fill":
                    self.on_alert(
                        Alert(
                            "critical",
                            f"{ev.client_tag}: fill for an order no "
                            "longer tracked: check the broker positions now",
                        )
                    )
                else:
                    log.warning("event for unknown tag %s ignored", ev.client_tag)
                continue
            t = self.positions[puid]
            m, now = t.machine, ev.at
            if ev.broker_order_id:
                self._known(ev.client_tag, ev.broker_order_id)
            if ev.kind == "ack":
                cmds = m.on_ack(now, ev.client_tag)
                o = m.orders[ev.client_tag]
                self.store.save_status(ev.client_tag, o.status, o.filled, "acknowledged")
            elif ev.kind == "fill":
                self.store.save_fill(ev.client_tag, ev.cum_filled, ev.avg_price, now)
                cmds = m.on_fill(now, ev.client_tag, ev.cum_filled, ev.avg_price or Decimal(0))
                o = m.orders[ev.client_tag]
                self.store.save_status(ev.client_tag, o.status, o.filled, "fill")
            elif ev.kind == "cancelled":
                if ev.cum_filled > m.orders[ev.client_tag].filled and ev.avg_price:
                    await self._feed(t, m.on_fill(now, ev.client_tag, ev.cum_filled, ev.avg_price))
                cmds = m.on_cancelled(now, ev.client_tag)
                self.store.save_status(
                    ev.client_tag,
                    OrderStatus.CANCELLED,
                    m.orders[ev.client_tag].filled,
                    "cancelled",
                )
            else:
                cmds = m.on_reject(now, ev.client_tag, ev.reason or "rejected")
                self.store.save_status(
                    ev.client_tag, OrderStatus.REJECTED, 0, ev.reason or "rejected"
                )
            await self._feed(t, cmds)
        await self._run_replays()

    async def tick(self) -> None:
        """Timers for every position (re-pricing, protection deadline, gap chase)."""
        now = self.now()
        for t in list(self.positions.values()):
            await self._feed(t, t.machine.on_timer(now))
            # an order still "sent" long after sending, with no broker id: look it up
            for o in list(t.machine.orders.values()):
                if (
                    o.status is OrderStatus.SENT
                    and o.tag not in self.oid
                    and o.last_change is not None
                    and (now - o.last_change).total_seconds() > 2 * self.send_timeout_s
                ):
                    await self._feed(t, t.machine.on_send_failed(now, o.tag, "no answer"))
        await self.pump()
