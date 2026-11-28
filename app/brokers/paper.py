"""Paper broker with a realistic fill model (brief section 16.2).

  * A buy LMT fills only when its limit is at or above the best ASK; a sell LMT only when
    its limit is at or below the best BID. The fill price is the touch (ask/bid), never
    better than the limit.
  * Each fill is capped by the displayed depth at the touch, so big orders fill in parts.
  * Orders are acknowledged after a configurable latency; until then they can't fill.
  * A sell SL-LMT triggers when the last price trades at/below its trigger, then works as
    an LMT at its limit. If the price has gapped through the limit it does NOT fill
    (the engine's gap chase, invariant 4, has to deal with it).
  * Rejections are simulated: wrong lot multiple, price off the tick grid, a price far
    outside the market (a "band"), plus an optional random rejection rate.
  * The same order lifecycle as a live broker: ack -> partial/fill -> cancelled/rejected,
    reported through the event sink. Positions and funds come from the fills.
Time is injected (`now`), so replay and tests are deterministic.

With `state_path` set, the order book is written to a JSON file (atomically) after every
change and read back on start, so a paper session survives a crash or `kill -9` like a
real broker account would, and restart reconciliation (invariant 7) can be tested.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import os
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
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
from app.core.types import InstrumentKey, OrderType, Product, Side


@dataclass(frozen=True)
class Depth:
    """Top of book for one instrument."""

    bid: Decimal | None
    ask: Decimal | None
    bid_qty: int
    ask_qty: int
    ltp: Decimal | None
    at: dt.datetime


@dataclass
class _PaperOrder:
    oid: str
    req: OrderRequest
    created: dt.datetime
    acked: bool = False
    status: BrokerOrderStatus = BrokerOrderStatus.PENDING
    filled: int = 0
    value: Decimal = Decimal(0)  # sum(price x qty) of fills
    triggered: bool = False
    limit: Decimal = Decimal(0)
    trigger: Decimal | None = None
    order_type: OrderType = OrderType.LMT
    qty: int = 0
    updated: dt.datetime | None = None

    @property
    def working(self) -> bool:
        return self.status in (
            BrokerOrderStatus.PENDING,
            BrokerOrderStatus.TRIGGER_PENDING,
            BrokerOrderStatus.PARTIAL,
        )

    @property
    def avg(self) -> Decimal | None:
        return (self.value / self.filled).quantize(Decimal("0.01")) if self.filled else None


@dataclass
class PaperConfig:
    latency_s: float = 0.2
    band_pct: float = 40.0  # reject limit prices this far from the last price (NSE option
    # operating ranges are wide and dynamic; 40% still catches a fat-fingered price)
    reject_rate: float = 0.0  # extra random rejections, for chaos tests
    seed: int = 7
    capital: Decimal = Decimal("50000")


@dataclass
class PaperBroker(BaseExecutionBroker):
    instruments: dict[InstrumentKey, InstrumentInfo]
    now: Callable[[], dt.datetime]
    cfg: PaperConfig = field(default_factory=PaperConfig)
    name: str = "paper"
    supports_modify_to_exit: bool = True
    state_path: Path | None = None

    def __post_init__(self) -> None:
        self._orders: dict[str, _PaperOrder] = {}
        self._by_tag: dict[str, str] = {}
        self._depth: dict[InstrumentKey, Depth] = {}
        self._seq = itertools.count(1)
        self._rnd = random.Random(self.cfg.seed)  # noqa: S311 - simulated rejections, not crypto
        self._cash_used = Decimal(0)
        self._dirty = False
        self._last_n = 0
        if self.state_path is not None and self.state_path.exists():
            self._load(self.state_path)

    def compact(self) -> int:
        """Forget finished orders of instruments that are flat with nothing working.
        Run at start-up so a long-lived paper book stays small. Returns orders dropped."""
        keep = {o.req.key for o in self._orders.values() if o.working}
        keep |= {o.req.key for o in self._orders.values() if self._long(o.req.key) != 0}
        drop = [oid for oid, o in self._orders.items() if o.req.key not in keep]
        for oid in drop:
            o = self._orders.pop(oid)
            self._by_tag.pop(o.req.client_tag, None)
        if drop:
            self._save()
        return len(drop)

    # ------------------------------------------------------------ persistence
    def _emit(self, ev: BrokerEvent) -> None:
        self._dirty = True
        super()._emit(ev)

    def _save(self) -> None:
        if self.state_path is None:
            return
        self._dirty = False
        rows: list[dict[str, Any]] = []
        for o in self._orders.values():
            rows.append(
                {
                    "oid": o.oid,
                    "req": o.req.model_dump(mode="json"),
                    "created": o.created.isoformat(),
                    "acked": o.acked,
                    "status": o.status.value,
                    "filled": o.filled,
                    "value": str(o.value),
                    "triggered": o.triggered,
                    "limit": str(o.limit),
                    "trigger": None if o.trigger is None else str(o.trigger),
                    "order_type": o.order_type.value,
                    "qty": o.qty,
                    "updated": o.updated.isoformat() if o.updated else None,
                }
            )
        n = self._last_n  # never reuse an id, even after compaction
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seq": n, "orders": rows}), encoding="utf-8")
        os.replace(tmp, self.state_path)  # atomic: a crash leaves the old or the new file

    def _load(self, path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for r in raw["orders"]:
            o = _PaperOrder(
                r["oid"],
                OrderRequest.model_validate(r["req"]),
                dt.datetime.fromisoformat(r["created"]),
                acked=r["acked"],
                status=BrokerOrderStatus(r["status"]),
                filled=r["filled"],
                value=Decimal(r["value"]),
                triggered=r["triggered"],
                limit=Decimal(r["limit"]),
                trigger=None if r["trigger"] is None else Decimal(r["trigger"]),
                order_type=OrderType(r["order_type"]),
                qty=r["qty"],
                updated=dt.datetime.fromisoformat(r["updated"]) if r["updated"] else None,
            )
            self._orders[o.oid] = o
            self._by_tag[o.req.client_tag] = o.oid
        self._last_n = int(raw["seq"])
        self._seq = itertools.count(self._last_n + 1)

    # ------------------------------------------------------------ provider basics
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.EXECUTION})

    async def health(self) -> bool:
        return True

    # ------------------------------------------------------------ market data in
    def on_market(self, key: InstrumentKey, depth: Depth) -> None:
        """Feed the latest top of book; matching runs immediately."""
        self._depth[key] = depth
        self.advance()

    def advance(self) -> None:
        """Acknowledge orders whose latency has passed and match everything."""
        now = self.now()
        for o in list(self._orders.values()):
            if not o.working:
                continue
            if not o.acked:
                if (now - o.created).total_seconds() < self.cfg.latency_s:
                    continue
                o.acked = True
                self._emit(
                    BrokerEvent(
                        kind="ack", client_tag=o.req.client_tag, broker_order_id=o.oid, at=now
                    )
                )
            self._match(o, now)
        if self._dirty:
            self._save()

    def _match(self, o: _PaperOrder, now: dt.datetime) -> None:
        d = self._depth.get(o.req.key)
        if d is None:
            return
        if o.order_type is OrderType.SL_LMT and not o.triggered:
            if o.trigger is None or d.ltp is None:
                return
            hit = d.ltp <= o.trigger if o.req.side is Side.SELL else d.ltp >= o.trigger
            if not hit:
                return
            o.triggered = True
        if o.req.side is Side.BUY:
            if d.ask is None or o.limit < d.ask or d.ask_qty <= 0:
                return
            px, avail = d.ask, d.ask_qty
        else:
            if d.bid is None or o.limit > d.bid or d.bid_qty <= 0:
                return  # includes a gap through the stop's limit: no fill
            px, avail = d.bid, d.bid_qty
        take = min(o.qty - o.filled, avail)
        if take <= 0:
            return
        o.filled += take
        o.value += px * take
        o.status = BrokerOrderStatus.FILLED if o.filled >= o.qty else BrokerOrderStatus.PARTIAL
        o.updated = now
        self._cash_used += px * take if o.req.side is Side.BUY else -px * take
        # consume displayed depth so the next order doesn't fill against the same size
        self._depth[o.req.key] = Depth(
            d.bid,
            d.ask,
            d.bid_qty - (take if o.req.side is Side.SELL else 0),
            d.ask_qty - (take if o.req.side is Side.BUY else 0),
            d.ltp,
            d.at,
        )
        self._emit(
            BrokerEvent(
                kind="fill",
                client_tag=o.req.client_tag,
                broker_order_id=o.oid,
                at=now,
                cum_filled=o.filled,
                avg_price=o.avg,
            )
        )

    # ------------------------------------------------------------ orders
    def _reject_reason(self, req: OrderRequest) -> str | None:
        info = self.instruments.get(req.key)
        if info is None:
            return "unknown instrument"
        if req.qty % info.lot_size:
            return f"quantity {req.qty} is not a multiple of lot {info.lot_size}"
        for p in (req.limit_price, req.trigger_price):
            if p is not None and (p / info.tick_size) % 1:
                return f"price {p} is not a multiple of tick {info.tick_size}"
        d = self._depth.get(req.key)
        # the band is a fat-finger check on prices meant to trade now; a protective
        # stop's limit sits far below the market by design (review finding)
        if d is not None and d.ltp and req.order_type is OrderType.LMT:
            dev = abs(req.limit_price - d.ltp) / d.ltp * 100
            if dev > Decimal(str(self.cfg.band_pct)):
                return f"limit {req.limit_price} is outside the price band"
        if req.side is Side.SELL and req.qty > self._long(req.key) - self._working_sells(req.key):
            return "sell exceeds open long position"  # brokers block naked option sells too
        if self.cfg.reject_rate and self._rnd.random() < self.cfg.reject_rate:
            return "random paper rejection"
        return None

    async def place_order(self, req: OrderRequest) -> PlaceResult:
        return self.place_sync(req)

    def place_sync(self, req: OrderRequest) -> PlaceResult:
        now = self.now()
        self._last_n = next(self._seq)
        oid = f"PAPER{self._last_n:08d}"
        why = self._reject_reason(req)  # before the order exists, so it isn't counted
        o = _PaperOrder(
            oid,
            req,
            now,
            limit=req.limit_price,
            trigger=req.trigger_price,
            order_type=req.order_type,
            qty=req.qty,
            updated=now,
        )
        self._orders[oid] = o
        self._by_tag[req.client_tag] = oid
        if why:
            o.status = BrokerOrderStatus.REJECTED
            self._emit(
                BrokerEvent(
                    kind="rejected",
                    client_tag=req.client_tag,
                    broker_order_id=oid,
                    at=now,
                    reason=why,
                )
            )
            self._save()
            return PlaceResult(outcome="REJECTED", broker_order_id=oid, message=why)
        if req.order_type is OrderType.SL_LMT:
            o.status = BrokerOrderStatus.TRIGGER_PENDING
        self._dirty = True
        self.advance()
        return PlaceResult(outcome="ACCEPTED", broker_order_id=oid)

    async def modify_order(
        self,
        broker_order_id: str,
        *,
        limit_price: Decimal | None = None,
        trigger_price: Decimal | None = None,
        qty: int | None = None,
        order_type: OrderType | None = None,
    ) -> PlaceResult:
        return self.modify_sync(
            broker_order_id,
            limit_price=limit_price,
            trigger_price=trigger_price,
            qty=qty,
            order_type=order_type,
        )

    def modify_sync(
        self,
        broker_order_id: str,
        *,
        limit_price: Decimal | None = None,
        trigger_price: Decimal | None = None,
        qty: int | None = None,
        order_type: OrderType | None = None,
    ) -> PlaceResult:
        o = self._orders.get(broker_order_id)
        if o is None or not o.working:
            return PlaceResult(
                outcome="REJECTED", broker_order_id=broker_order_id, message="order not open"
            )
        if qty is not None:
            if qty < o.filled:
                return PlaceResult(
                    outcome="REJECTED",
                    broker_order_id=broker_order_id,
                    message="quantity below filled",
                )
            if o.req.side is Side.SELL and qty - o.filled > (
                self._long(o.req.key) - self._working_sells(o.req.key) + o.qty - o.filled
            ):
                return PlaceResult(
                    outcome="REJECTED",
                    broker_order_id=broker_order_id,
                    message="sell exceeds open long position",
                )
            o.qty = qty
        if limit_price is not None:
            o.limit = limit_price
        if order_type is not None:
            o.order_type = order_type
            if order_type is OrderType.LMT:
                o.trigger, o.triggered = None, True
                if o.status is BrokerOrderStatus.TRIGGER_PENDING:
                    o.status = BrokerOrderStatus.PENDING
        if trigger_price is not None:
            o.trigger = trigger_price
        o.updated = self.now()
        self._dirty = True
        self.advance()
        return PlaceResult(outcome="ACCEPTED", broker_order_id=broker_order_id)

    async def cancel_order(self, broker_order_id: str) -> PlaceResult:
        return self.cancel_sync(broker_order_id)

    def cancel_sync(self, broker_order_id: str) -> PlaceResult:
        o = self._orders.get(broker_order_id)
        if o is None or not o.working:
            return PlaceResult(
                outcome="REJECTED",
                broker_order_id=broker_order_id,
                message="order not open (already filled or cancelled)",
            )
        o.status = BrokerOrderStatus.CANCELLED
        o.updated = self.now()
        self._emit(
            BrokerEvent(
                kind="cancelled",
                client_tag=o.req.client_tag,
                broker_order_id=broker_order_id,
                at=o.updated,
                cum_filled=o.filled,
                avg_price=o.avg,
            )
        )
        self._save()
        return PlaceResult(outcome="ACCEPTED", broker_order_id=broker_order_id)

    def oid_for(self, tag: str) -> str | None:
        return self._by_tag.get(tag)

    # ------------------------------------------------------------ account state
    def _long(self, key: InstrumentKey) -> int:
        return sum(
            (o.filled if o.req.side is Side.BUY else -o.filled)
            for o in self._orders.values()
            if o.req.key == key
        )

    def _working_sells(self, key: InstrumentKey) -> int:
        return sum(
            o.qty - o.filled
            for o in self._orders.values()
            if o.req.key == key and o.req.side is Side.SELL and o.working
        )

    async def order_book(self) -> Sequence[BrokerOrder]:
        return [
            BrokerOrder(
                broker_order_id=o.oid,
                client_tag=o.req.client_tag,
                key=o.req.key,
                side=o.req.side,
                qty=o.qty,
                filled_qty=o.filled,
                avg_fill_price=o.avg,
                limit_price=o.limit,
                trigger_price=o.trigger,
                order_type=o.order_type.value,
                status=o.status,
                updated_at=o.updated,
            )
            for o in self._orders.values()
        ]

    async def positions(self) -> Sequence[BrokerPosition]:
        out = []
        for key in {o.req.key for o in self._orders.values()}:
            q = self._long(key)
            if q:
                buys = [
                    o
                    for o in self._orders.values()
                    if o.req.key == key and o.req.side is Side.BUY and o.filled
                ]
                bq = sum(o.filled for o in buys)
                avg = (sum((o.value for o in buys), Decimal(0)) / bq) if bq else None
                info = self.instruments.get(key)
                out.append(
                    BrokerPosition(
                        key=key,
                        broker_symbol=info.broker_symbol if info else key.canonical,
                        product=Product.MIS,
                        net_qty=q,
                        avg_price=avg,
                    )
                )
        return out

    async def holdings(self) -> Sequence[BrokerHolding]:
        return []

    async def funds(self) -> Funds:
        return Funds(available_cash=self.cfg.capital - self._cash_used, used_margin=self._cash_used)

    async def instrument_master(self, trading_date: dt.date) -> Sequence[InstrumentInfo]:
        return list(self.instruments.values())
