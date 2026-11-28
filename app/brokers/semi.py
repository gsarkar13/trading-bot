"""SEMI (manual) broker: the engine's orders become instructions for the trader (5.4, inv 15).

Every instruction is an *order task* with a short id (T0001...). The task for an entry
also shows the protective SL-LMT to place right after the fill: one instruction, both
orders. Nothing is assumed: a position exists only once the trader confirms the fill
("Placed at ₹___") or, where the broker's order book can be read from this host, the
book shows it. Self-confirmed fills are flagged SELF_REPORTED.

If no protective stop has been confirmed `protect_timeout_s` after a confirmed entry,
an alert is raised and repeated every minute until it is (invariant 15).

Trader actions (Telegram buttons or the dashboard) call:
    confirm_entry(task, price)   confirm_stop(task)   confirm_exit(task, price)   skip(task)
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.adapters.base import (
    BaseExecutionBroker,
    BrokerEvent,
    BrokerHolding,
    BrokerOrder,
    BrokerOrderStatus,
    BrokerPosition,
    Capability,
    Funds,
    InstrumentInfo,
    OrderRequest,
    PlaceResult,
)
from app.config.schema import ExecutionMechanics
from app.core.types import InstrumentKey, OrderType, Side
from app.engine.oms import Alert


def semi_mechanics(base: ExecutionMechanics, task_expiry_s: float = 300) -> ExecutionMechanics:
    """A human can't re-price every 2 seconds. In SEMI: no re-pricing, the entry task
    expires after `task_expiry_s`, no automatic flatten for a missing stop (escalating
    alerts instead, invariant 15), and no automatic gap chase."""
    return base.model_copy(
        update={
            "reprice_after_s": task_expiry_s,
            "max_reprices": 0,
            "protect_fill_within_s": 10**9,
            "gap_chase_after_s": 10**9,
        }
    )


@dataclass
class SemiTask:
    uid: str
    entry_tag: str
    key: InstrumentKey
    symbol: str
    qty: int
    entry_limit: Decimal
    planned_stop: tuple[Decimal, Decimal] | None  # (trigger, limit) shown with the entry
    created: dt.datetime
    status: str = "SENT"  # SENT -> CONFIRMED -> STOP_SEEN -> EXITED | SKIPPED | EXPIRED
    fill_price: Decimal | None = None
    confirmed_at: dt.datetime | None = None
    stop_tag: str | None = None
    stop_seen_at: dt.datetime | None = None
    exit_tag: str | None = None
    exit_price: Decimal | None = None
    last_escalation: dt.datetime | None = None
    self_reported: bool = True

    def message(self) -> str:
        lines = [
            f"ORDER TASK {self.uid}",
            f"BUY {self.qty} x {self.symbol}  LIMIT {self.entry_limit}",
        ]
        if self.planned_stop:
            trig, lim = self.planned_stop
            lines.append(f"Then place STOP: SELL {self.qty} SL-LMT  trigger {trig}  limit {lim}")
        lines.append("Reply with the price you got, or Skip.")
        return "\n".join(lines)


@dataclass
class SemiBroker(BaseExecutionBroker):
    instruments: dict[InstrumentKey, InstrumentInfo]
    now: Callable[[], dt.datetime]
    on_task: Callable[[SemiTask, str], None]  # (task, event) -> Telegram + dashboard
    on_alert: Callable[[Alert], None]
    protect_timeout_s: float = 120
    escalate_every_s: float = 60
    name: str = "semi"
    supports_modify_to_exit: bool = False
    tasks: dict[str, SemiTask] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._seq = itertools.count(1)
        self._by_tag: dict[str, SemiTask] = {}
        self._plans: dict[str, tuple[Decimal, Decimal]] = {}
        self._orders: dict[str, dict[str, Any]] = {}

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.EXECUTION})

    async def health(self) -> bool:
        return True

    def plan_stop(self, entry_tag: str, trigger: Decimal, limit: Decimal) -> None:
        """The engine tells us the protective stop before the entry goes out."""
        self._plans[entry_tag] = (trigger, limit)

    # ------------------------------------------------------------ engine -> trader
    async def place_order(self, req: OrderRequest) -> PlaceResult:
        now = self.now()
        info = self.instruments.get(req.key)
        if info is None:
            return PlaceResult(outcome="REJECTED", message="unknown instrument")
        self._orders[req.client_tag] = {
            "req": req,
            "status": BrokerOrderStatus.PENDING,
            "filled": 0,
            "avg": None,
            "updated": now,
        }
        if req.side is Side.BUY:
            t = SemiTask(
                f"T{next(self._seq):04d}",
                req.client_tag,
                req.key,
                info.broker_symbol,
                req.qty,
                req.limit_price,
                self._plans.get(req.client_tag),
                now,
            )
            self.tasks[t.uid] = t
            self._by_tag[req.client_tag] = t
            self.on_task(t, "new")
            # Delivered to the trader = working (so it can expire if nobody acts on it).
            self._emit(
                BrokerEvent(kind="ack", client_tag=req.client_tag, broker_order_id=t.uid, at=now)
            )
            return PlaceResult(outcome="ACCEPTED", broker_order_id=t.uid)
        task = next(
            (
                x
                for x in self.tasks.values()
                if x.key == req.key and x.status in ("CONFIRMED", "STOP_SEEN")
            ),
            None,
        )
        if task is None:
            return PlaceResult(outcome="REJECTED", message="no confirmed position to sell")
        self._by_tag[req.client_tag] = task
        if req.order_type is OrderType.SL_LMT:
            task.stop_tag = req.client_tag  # waits for the trader's "stop placed"
            self.on_task(task, "stop_requested")
        else:
            task.exit_tag = req.client_tag
            self.on_task(task, "exit_requested")
        return PlaceResult(outcome="ACCEPTED", broker_order_id=f"{task.uid}-{req.client_tag}")

    async def modify_order(
        self,
        broker_order_id: str,
        *,
        limit_price: Decimal | None = None,
        trigger_price: Decimal | None = None,
        qty: int | None = None,
        order_type: OrderType | None = None,
    ) -> PlaceResult:
        return PlaceResult(outcome="REJECTED", message="SEMI orders are changed by the trader")

    async def cancel_order(self, broker_order_id: str) -> PlaceResult:
        tag = broker_order_id.split("-", 1)[1] if "-" in broker_order_id else None
        task = self.tasks.get(broker_order_id.split("-", 1)[0])
        if task is None:
            return PlaceResult(outcome="REJECTED", message="unknown task")
        if tag is None:  # the entry task itself: expired or withdrawn
            if task.status == "SENT":
                task.status = "EXPIRED"
                self._cancelled(task.entry_tag)
                self.on_task(task, "expired")
            return PlaceResult(outcome="ACCEPTED", broker_order_id=broker_order_id)
        if tag == task.stop_tag:
            # Engine wants out: tell the trader. Settled when the trader reports the exit.
            self.on_task(task, "exit_requested")
            return PlaceResult(outcome="ACCEPTED", broker_order_id=broker_order_id)
        return PlaceResult(outcome="REJECTED", message="order not open")

    # ------------------------------------------------------------ trader -> engine
    def confirm_entry(self, uid: str, price: Decimal, qty: int | None = None) -> None:
        t = self.tasks[uid]
        if t.status != "SENT":
            raise ValueError(f"task {uid} is {t.status}")
        t.status, t.fill_price, t.confirmed_at = "CONFIRMED", price, self.now()
        self._fill(t.entry_tag, qty or t.qty, price)  # already acked when delivered

    def confirm_stop(self, uid: str) -> None:
        t = self.tasks[uid]
        if t.stop_tag is None:
            raise ValueError(f"task {uid}: no stop requested yet")
        t.status, t.stop_seen_at = "STOP_SEEN", self.now()
        self._orders[t.stop_tag]["status"] = BrokerOrderStatus.TRIGGER_PENDING
        self._emit(
            BrokerEvent(
                kind="ack",
                client_tag=t.stop_tag,
                broker_order_id=f"{uid}-{t.stop_tag}",
                at=self.now(),
            )
        )

    def confirm_exit(self, uid: str, price: Decimal) -> None:
        """Trader sold (and removed the stop). Reported as the open sell order filling at
        the trader's price: the exit order if the engine sent one, else the stop. Either
        way the position closes once, and no new stop is requested."""
        t = self.tasks[uid]
        t.exit_price = price
        tag = t.exit_tag or t.stop_tag
        if tag is None:
            raise ValueError(f"task {uid}: no open sell order to settle")
        self._fill(tag, self._orders[tag]["req"].qty, price)
        t.status = "EXITED"
        self.on_task(t, "exited")

    def stop_hit(self, uid: str, price: Decimal) -> None:
        """The broker filled the trader's stop."""
        t = self.tasks[uid]
        if t.stop_tag:
            self._fill(t.stop_tag, self._orders[t.stop_tag]["req"].qty, price)
        t.status = "EXITED"

    def skip(self, uid: str) -> None:
        t = self.tasks[uid]
        if t.status == "SENT":
            t.status = "SKIPPED"
            self._cancelled(t.entry_tag)
            self.on_task(t, "skipped")

    def _fill(self, tag: str, qty: int, price: Decimal) -> None:
        o = self._orders[tag]
        o["filled"], o["avg"], o["status"] = qty, price, BrokerOrderStatus.FILLED
        self._emit(
            BrokerEvent(
                kind="fill",
                client_tag=tag,
                broker_order_id=tag,
                at=self.now(),
                cum_filled=qty,
                avg_price=price,
            )
        )

    def _cancelled(self, tag: str) -> None:
        self._orders[tag]["status"] = BrokerOrderStatus.CANCELLED
        self._emit(
            BrokerEvent(kind="cancelled", client_tag=tag, broker_order_id=tag, at=self.now())
        )

    # ------------------------------------------------------------ escalation (inv 15)
    def tick(self) -> None:
        now = self.now()
        for t in self.tasks.values():
            if t.status != "CONFIRMED" or t.confirmed_at is None:
                continue
            waited = (now - t.confirmed_at).total_seconds()
            if waited < self.protect_timeout_s:
                continue
            if (
                t.last_escalation is None
                or (now - t.last_escalation).total_seconds() >= self.escalate_every_s
            ):
                t.last_escalation = now
                stop = (
                    f" trigger {t.planned_stop[0]} limit {t.planned_stop[1]}"
                    if t.planned_stop
                    else ""
                )
                self.on_alert(
                    Alert(
                        "critical",
                        f"{t.uid}: NO PROTECTIVE STOP confirmed "
                        f"{int(waited)}s after entry. Place SELL "
                        f"{t.qty} {t.symbol} SL-LMT{stop} now.",
                    )
                )

    # ------------------------------------------------------------ account state
    async def order_book(self) -> Sequence[BrokerOrder]:
        out = []
        for tag, o in self._orders.items():
            r: OrderRequest = o["req"]
            out.append(
                BrokerOrder(
                    broker_order_id=tag,
                    client_tag=tag,
                    key=r.key,
                    side=r.side,
                    qty=r.qty,
                    filled_qty=o["filled"],
                    avg_fill_price=o["avg"],
                    limit_price=r.limit_price,
                    trigger_price=r.trigger_price,
                    order_type=r.order_type.value,
                    status=o["status"],
                    updated_at=o["updated"],
                )
            )
        return out

    async def positions(self) -> Sequence[BrokerPosition]:
        return []  # SEMI positions are self-reported; see the ledger

    async def holdings(self) -> Sequence[BrokerHolding]:
        return []

    async def funds(self) -> Funds:
        return Funds(available_cash=Decimal(0))

    async def instrument_master(self, trading_date: dt.date) -> Sequence[InstrumentInfo]:
        return list(self.instruments.values())
