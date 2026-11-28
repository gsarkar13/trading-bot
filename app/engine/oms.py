"""Order/position state machine (brief section 9.3; invariants 1-5).

One `PositionMachine` per position. It is PURE: it never talks to a broker or a database.
The engine feeds it events (`on_*` methods) and executes the `Command`s it returns:

    events in:  ack, reject, fill (cumulative), cancelled, send failed, lookup result,
                option quote, timer tick, exit request
    commands:   Place, Modify, Cancel, Lookup (look an order tag up in the order book), Alert

The same machine runs in PAPER, SEMI and LIVE; only the adapter that executes the
commands differs. Every state change is recorded in `transitions` with its reason.

Invariants enforced here (each also has a property test that tries to break it):
  1. never short: a sell is never larger than the broker-confirmed long quantity
  2. at most one working exit-side order (PROTECT/EXIT/FLATTEN) at any moment
  3. a filled quantity gets a broker-side SL-LMT within N seconds, else it is flattened
  4. a triggered stop that hasn't filled is chased toward the market in steps
  5. each order intent has a unique tag recorded BEFORE it is sent; after a timeout the
     tag is looked up in the order book, and only an order confirmed absent is re-sent
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from app.config.schema import ExecutionMechanics
from app.core.types import (
    EXIT_PURPOSES,
    WORKING_ORDER_STATUSES,
    OrderStatus,
    OrderType,
    PositionState,
    Side,
)

Purpose = Literal["ENTRY", "PROTECT", "EXIT", "FLATTEN"]


# ---------------------------------------------------------------- commands
@dataclass(frozen=True)
class Place:
    tag: str
    purpose: Purpose
    side: Side
    qty: int
    order_type: OrderType
    limit: Decimal
    trigger: Decimal | None = None


@dataclass(frozen=True)
class Modify:
    tag: str
    limit: Decimal | None = None
    trigger: Decimal | None = None
    qty: int | None = None
    order_type: OrderType | None = None


@dataclass(frozen=True)
class Cancel:
    tag: str


@dataclass(frozen=True)
class Lookup:
    tag: str


@dataclass(frozen=True)
class Alert:
    level: Literal["info", "warn", "critical"]
    message: str


Command = Place | Modify | Cancel | Lookup | Alert


# ---------------------------------------------------------------- order record
@dataclass
class Order:
    tag: str
    purpose: Purpose
    side: Side
    qty: int
    order_type: OrderType
    limit: Decimal
    trigger: Decimal | None
    status: OrderStatus = OrderStatus.NEW
    filled: int = 0
    avg_price: Decimal | None = None
    created_at: dt.datetime | None = None
    last_change: dt.datetime | None = None
    reprices: int = 0
    first_limit: Decimal | None = None
    sends: int = 0
    triggered_at: dt.datetime | None = None  # option traded through the SL trigger
    chases: int = 0
    cancel_requested: bool = False

    @property
    def working(self) -> bool:
        return self.status in WORKING_ORDER_STATUSES

    @property
    def remaining(self) -> int:
        return self.qty - self.filled


@dataclass(frozen=True)
class Quote:
    bid: Decimal | None
    ask: Decimal | None
    ltp: Decimal | None
    at: dt.datetime


@dataclass
class Transition:
    at: dt.datetime
    from_state: str
    to_state: str
    reason: str


class InvariantViolation(AssertionError):
    """Raised before a command that would break a safety invariant leaves the machine."""


# ---------------------------------------------------------------- the machine
@dataclass
class PositionMachine:
    position_uid: str
    lot_size: int
    tick: Decimal
    target_qty: int
    backstop_trigger: Decimal  # broker-side SL-LMT trigger on the option premium
    cfg: ExecutionMechanics
    new_tag: Callable[[], str]
    modify_to_exit: bool = True  # proposal P2: turn the resting SL into the exit order
    sl_limit_offset_ticks: int = 10
    state: PositionState = PositionState.SIGNALLED
    orders: dict[str, Order] = field(default_factory=dict)
    transitions: list[Transition] = field(default_factory=list)
    first_fill_at: dt.datetime | None = None
    exit_reason: str | None = None
    last_quote: Quote | None = None
    exit_attempts: int = 0

    # ------------------------------------------------------------ restart (invariant 7)
    @classmethod
    def adopt(
        cls,
        position_uid: str,
        lot_size: int,
        tick: Decimal,
        cfg: ExecutionMechanics,
        new_tag: Callable[[], str],
        now: dt.datetime,
        orders: list[Order],
        broker_long: int,
        broker_avg: Decimal | None,
        backstop_trigger: Decimal,
    ) -> PositionMachine:
        """Rebuild a machine after a restart from the broker's view, which is the truth.

        `orders` are our own orders for this position as the broker reports them now.
        If the broker's long quantity is not explained by those orders (a manual trade,
        a lost record), a synthetic filled BUY tagged ADOPT-... makes up the difference so
        the never-short guard works on what the broker actually holds.
        The machine starts UNMANAGED unless a working stop already covers the position;
        the engine then protects or flattens it (`execution.unmanaged_action`).
        """
        m = cls(position_uid, lot_size, tick, max(broker_long, 1), backstop_trigger, cfg, new_tag)
        for o in orders:
            m.orders[o.tag] = o
        missing = broker_long - m.long_qty
        if missing > 0:
            px = broker_avg or backstop_trigger
            m.orders[f"ADOPT-{position_uid}"] = Order(
                f"ADOPT-{position_uid}",
                "ENTRY",
                Side.BUY,
                missing,
                OrderType.LMT,
                px,
                None,
                status=OrderStatus.FILLED,
                filled=missing,
                avg_price=px,
                created_at=now,
                last_change=now,
                first_limit=px,
            )
        m.first_fill_at = now if broker_long > 0 else None
        p = m.protect
        if broker_long <= 0:
            if not m.working():
                m._set(now, PositionState.CLOSED, "restart: nothing held at the broker")
            else:
                m._set(now, PositionState.EXITING, "restart: flat with working orders")
                m.exit_reason = "restart"
        elif m.working("EXIT", "FLATTEN"):
            m._set(now, PositionState.EXITING, "restart: exit was in progress")
            m.exit_reason = "restart"
        elif (
            p is not None and p.status is OrderStatus.TRIGGER_PENDING and p.remaining == broker_long
        ):
            m._set(now, PositionState.OPEN_PROTECTED, "restart: held and protected")
        else:
            m._set(now, PositionState.UNMANAGED, "restart: held without a covering stop")
        return m

    def take_over(self, now: dt.datetime, action: str) -> list[Command]:
        """UNMANAGED -> protected (or flattened). No-op in any other state."""
        if self.state is not PositionState.UNMANAGED:
            return []
        if action == "flatten":
            return self._flatten(now, "unmanaged at restart")
        self._set(now, PositionState.PARTIAL, "unmanaged: placing a protective stop")
        self.first_fill_at = now  # the protection deadline runs from now
        return self._ensure_protected(now)

    # ------------------------------------------------------------ derived quantities
    @property
    def bought(self) -> int:
        return sum(o.filled for o in self.orders.values() if o.side is Side.BUY)

    @property
    def sold(self) -> int:
        return sum(o.filled for o in self.orders.values() if o.side is Side.SELL)

    @property
    def long_qty(self) -> int:
        """Broker-confirmed long quantity: filled buys minus filled sells."""
        return self.bought - self.sold

    def working(self, *purposes: str) -> list[Order]:
        ps = purposes or ("ENTRY", "PROTECT", "EXIT", "FLATTEN")
        return [o for o in self.orders.values() if o.working and o.purpose in ps]

    @property
    def protect(self) -> Order | None:
        w = self.working("PROTECT")
        return w[0] if w else None

    def _maybe_open(self, now: dt.datetime) -> None:
        p = self.protect
        if (
            self.state in (PositionState.ENTRY_PENDING, PositionState.PARTIAL)
            and not self.working("ENTRY")
            and self.long_qty > 0
            and p is not None
            and p.status is OrderStatus.TRIGGER_PENDING
            and p.remaining == self.long_qty
        ):
            self._set(now, PositionState.OPEN_PROTECTED, "filled and protected")

    def _set(self, now: dt.datetime, state: PositionState, reason: str) -> None:
        if state is not self.state:
            self.transitions.append(Transition(now, self.state.value, state.value, reason))
            self.state = state

    # ------------------------------------------------------------ price helpers
    def _round(self, p: Decimal, up: bool) -> Decimal:
        n = p / self.tick
        steps = math.ceil(n) if up else math.floor(n)
        return max(self.tick, Decimal(steps) * self.tick)

    def _buy_limit(self, q: Quote) -> Decimal | None:
        ref = q.ask or q.ltp
        if ref is None:
            return None
        return self._round(ref + self.cfg.entry_buffer_ticks * self.tick, up=True)

    def _sell_limit(self, q: Quote | None, fallback: Decimal) -> Decimal:
        ref = (q.bid or q.ltp) if q is not None else None
        ref = ref if ref is not None else fallback
        return self._round(ref - self.cfg.exit_buffer_ticks * self.tick, up=False)

    def _sl_limit(self) -> Decimal:
        return self._round(self.backstop_trigger - self.sl_limit_offset_ticks * self.tick, up=False)

    # ------------------------------------------------------------ order creation
    def _new_order(
        self,
        now: dt.datetime,
        purpose: Purpose,
        side: Side,
        qty: int,
        otype: OrderType,
        limit: Decimal,
        trigger: Decimal | None = None,
    ) -> Place:
        if side is Side.SELL:
            self._guard_sell(qty)
        tag = self.new_tag()
        if tag in self.orders:
            raise InvariantViolation(f"duplicate tag {tag}")  # invariant 5
        o = Order(
            tag,
            purpose,
            side,
            qty,
            otype,
            limit,
            trigger,
            created_at=now,
            last_change=now,
            first_limit=limit,
            sends=1,
        )
        o.status = OrderStatus.SENT  # stored (NEW) by the engine before this command runs
        self.orders[tag] = o
        if purpose in EXIT_PURPOSES:
            self._guard_one_exit()
        return Place(tag, purpose, side, qty, otype, limit, trigger)

    def _guard_sell(self, qty: int) -> None:
        """Invariant 1: never sell more than the broker-confirmed long quantity."""
        open_sells = sum(o.remaining for o in self.working() if o.side is Side.SELL)
        if qty <= 0 or qty + open_sells > self.long_qty:
            raise InvariantViolation(
                f"sell {qty} with {open_sells} already working exceeds long {self.long_qty}"
            )

    def _guard_one_exit(self) -> None:
        """Invariant 2: at most one working exit-side order."""
        if len(self.working(*EXIT_PURPOSES)) > 1:
            raise InvariantViolation("two working exit-side orders")

    # ------------------------------------------------------------ entry
    def start_entry(self, now: dt.datetime, quote: Quote) -> list[Command]:
        if self.state is not PositionState.SIGNALLED:
            return []
        self.last_quote = quote
        limit = self._buy_limit(quote)
        if limit is None:
            self._set(now, PositionState.ERROR, "no quote to price the entry")
            return [Alert("warn", f"{self.position_uid}: entry skipped, no quote")]
        cmd = self._new_order(now, "ENTRY", Side.BUY, self.target_qty, OrderType.LMT, limit)
        self._set(now, PositionState.ENTRY_PENDING, f"entry LMT {limit} sent")
        return [cmd]

    # ------------------------------------------------------------ events
    def on_ack(self, now: dt.datetime, tag: str) -> list[Command]:
        o = self.orders.get(tag)
        if o is None or not o.working:
            return []
        if o.status in (OrderStatus.SENT, OrderStatus.UNCERTAIN):
            o.status = (
                OrderStatus.TRIGGER_PENDING
                if o.order_type is OrderType.SL_LMT
                else OrderStatus.PENDING
            )
            o.last_change = now
        self._maybe_open(now)
        return []

    def on_reject(self, now: dt.datetime, tag: str, reason: str) -> list[Command]:
        o = self.orders.get(tag)
        if o is None or not o.working:
            return []
        o.status = OrderStatus.REJECTED
        o.last_change = now
        if o.purpose == "ENTRY":
            if self.long_qty == 0:
                self._set(now, PositionState.REJECTED, f"entry rejected: {reason}")
                return []
            return self._ensure_protected(now)
        if o.purpose == "PROTECT":
            # Invariant 3: can't protect -> flatten
            return [
                Alert(
                    "critical",
                    f"{self.position_uid}: protective stop rejected ({reason}); flattening",
                ),
                *self._flatten(now, reason),
            ]
        # exit / flatten rejected: try again with a fresh order
        return self._retry_exit(now, f"exit rejected: {reason}")

    def on_cancelled(self, now: dt.datetime, tag: str) -> list[Command]:
        o = self.orders.get(tag)
        if o is None or not o.working:
            return []
        o.status = OrderStatus.CANCELLED
        o.last_change = now
        cmds: list[Command] = []
        if (
            o.purpose == "ENTRY"
            and self.long_qty == 0
            and self.state is not (PositionState.EXITING)
        ):
            self._set(now, PositionState.CLOSED, "entry cancelled unfilled")
            return []
        if self.state is PositionState.EXITING:
            if self.long_qty > 0 and not self.working(*EXIT_PURPOSES):
                cmds += self._place_exit(now)  # the resting SL is gone: now send the exit
            elif self.long_qty <= 0 and not self.working():
                self._set(now, PositionState.CLOSED, "flat after cancel")
        elif self.long_qty > 0:
            if o.purpose == "PROTECT":
                cmds.append(
                    Alert(
                        "critical",
                        f"{tag}: protective stop cancelled outside an exit; re-protecting",
                    )
                )
            cmds += self._ensure_protected(now)
        self._maybe_open(now)
        return cmds

    def on_fill(
        self, now: dt.datetime, tag: str, cum_filled: int, avg_price: Decimal
    ) -> list[Command]:
        """`cum_filled` is the order's total filled quantity so far (broker style)."""
        o = self.orders.get(tag)
        if o is None or cum_filled <= o.filled:
            return []  # unknown, duplicate or out-of-order fill report
        o.filled = min(cum_filled, o.qty)
        o.avg_price = avg_price
        o.last_change = now
        if o.filled >= o.qty:
            o.status = OrderStatus.FILLED
        elif o.status is not OrderStatus.CANCELLED:
            o.status = OrderStatus.PARTIAL
        cmds: list[Command] = []
        if o.side is Side.BUY:
            if self.first_fill_at is None:
                self.first_fill_at = now
            if self.state in (PositionState.ENTRY_PENDING, PositionState.SIGNALLED):
                self._set(
                    now,
                    PositionState.PARTIAL if o.working else PositionState.ENTRY_PENDING,
                    f"entry filled {o.filled}/{o.qty}",
                )
            if self.state is PositionState.EXITING:
                cmds += self._cover_late_fill(now)  # entry filled while we were exiting
            else:
                cmds += self._ensure_protected(now)
            self._maybe_open(now)
        else:
            if self.long_qty <= 0:
                for w in self.working():
                    cmds.append(self._cancel(now, w))
                self._set(now, PositionState.CLOSED, f"{o.purpose.lower()} filled; position flat")
            elif o.purpose == "PROTECT" and self.state is not PositionState.EXITING:
                self._set(now, PositionState.EXITING, "protective stop filling")
            elif self.state is PositionState.EXITING and not self.working(*EXIT_PURPOSES):
                cmds += self._place_exit(now)  # exit filled only part: send the rest
        return cmds

    def _cover_late_fill(self, now: dt.datetime) -> list[Command]:
        exits = self.working(*EXIT_PURPOSES)
        if not exits:
            return self._place_exit(now)
        x = exits[0]
        if x.remaining < self.long_qty and x.status is not OrderStatus.UNCERTAIN:
            x.qty = x.filled + self.long_qty
            x.last_change = now
            return [Modify(x.tag, qty=x.qty)]
        return []

    def on_send_failed(self, now: dt.datetime, tag: str, reason: str) -> list[Command]:
        """Timeout / network error: we don't know if the broker has it (invariant 5)."""
        o = self.orders.get(tag)
        if o is None or not o.working:
            return []
        o.status = OrderStatus.UNCERTAIN
        o.last_change = now
        return [Lookup(tag), Alert("warn", f"{tag}: send uncertain ({reason}); looking it up")]

    def on_lookup(
        self,
        now: dt.datetime,
        tag: str,
        found: bool,
        *,
        filled: int = 0,
        avg_price: Decimal | None = None,
        broker_status: str = "",
    ) -> list[Command]:
        o = self.orders.get(tag)
        if o is None or o.status is not OrderStatus.UNCERTAIN:
            return []
        if found:
            cmds = self.on_ack(now, tag)
            if filled > o.filled and avg_price is not None:
                cmds += self.on_fill(now, tag, filled, avg_price)
            if broker_status in ("CANCELLED", "REJECTED"):
                cmds += (
                    self.on_cancelled(now, tag)
                    if broker_status == "CANCELLED"
                    else self.on_reject(now, tag, "found rejected")
                )
            return cmds
        # Confirmed absent: re-send under the SAME tag (idempotent), at most twice.
        if o.sends >= 3:
            o.status = OrderStatus.REJECTED
            return [
                Alert("critical", f"{tag}: not at broker after 3 sends; giving up"),
                *(self._retry_exit(now, "send failed") if o.purpose != "ENTRY" else []),
            ]
        if o.side is Side.SELL:
            o.status = OrderStatus.CANCELLED  # exclude itself from the guard, then re-check
            self._guard_sell(o.remaining)
        o.status = OrderStatus.SENT
        o.sends += 1
        return [Place(o.tag, o.purpose, o.side, o.remaining, o.order_type, o.limit, o.trigger)]

    def on_quote(self, now: dt.datetime, q: Quote) -> list[Command]:
        self.last_quote = q
        p = self.protect
        if (
            p is not None
            and p.trigger is not None
            and q.ltp is not None
            and q.ltp <= p.trigger
            and p.triggered_at is None
        ):
            p.triggered_at = now
        return []

    # ------------------------------------------------------------ exits
    def request_exit(self, now: dt.datetime, reason: str) -> list[Command]:
        """Target / strategy stop hit, square-off, manual flatten."""
        if self.state in (PositionState.CLOSED, PositionState.REJECTED):
            return []
        self.exit_reason = self.exit_reason or reason
        cmds: list[Command] = [self._cancel(now, e) for e in self.working("ENTRY")]
        if self.long_qty <= 0:
            if not self.working("ENTRY"):
                self._set(now, PositionState.CLOSED, f"{reason}; nothing filled")
            return cmds
        self._set(now, PositionState.EXITING, reason)
        p = self.protect
        if p is not None:
            if self.modify_to_exit and p.status is not OrderStatus.UNCERTAIN:
                # P2: the resting stop becomes the (single) exit order
                limit = self._sell_limit(self.last_quote, p.limit)
                p.purpose = "EXIT"
                p.order_type = OrderType.LMT
                p.limit, p.trigger, p.last_change, p.reprices = limit, None, now, 0
                p.first_limit = limit
                cmds.append(Modify(p.tag, limit=limit, order_type=OrderType.LMT))
                return cmds
            cmds.append(self._cancel(now, p))  # invariant 2: cancel, confirm, THEN exit
            return cmds
        if not self.working(*EXIT_PURPOSES):
            cmds += self._place_exit(now)
        return cmds

    def _place_exit(self, now: dt.datetime, purpose: Purpose = "EXIT") -> list[Command]:
        qty = self.long_qty
        if qty <= 0 or self.working(*EXIT_PURPOSES):
            return []
        fallback = (
            self.last_quote.ltp
            if self.last_quote and self.last_quote.ltp
            else self.backstop_trigger
        )
        limit = self._sell_limit(self.last_quote, fallback)
        return [self._new_order(now, purpose, Side.SELL, qty, OrderType.LMT, limit)]

    def _retry_exit(self, now: dt.datetime, why: str) -> list[Command]:
        self.exit_attempts += 1
        if self.exit_attempts > self.cfg.flatten_max_attempts:
            return [
                Alert(
                    "critical",
                    f"{self.position_uid}: could not exit after "
                    f"{self.exit_attempts - 1} attempts ({why}); "
                    "EXIT MANUALLY NOW",
                )
            ]
        self._set(now, PositionState.EXITING, why)
        return self._place_exit(now, "FLATTEN" if self.exit_reason == "flatten" else "EXIT")

    def _flatten(self, now: dt.datetime, why: str) -> list[Command]:
        self.exit_reason = "flatten"
        cmds: list[Command] = [self._cancel(now, o) for o in self.working() if o.side is Side.BUY]
        self._set(now, PositionState.EXITING, f"flatten: {why}")
        if self.working(*EXIT_PURPOSES):
            for o in self.working(*EXIT_PURPOSES):
                cmds.append(self._cancel(now, o))
            return cmds  # exit goes out once the cancel is confirmed
        return cmds + self._place_exit(now, "FLATTEN")

    def _cancel(self, now: dt.datetime, o: Order) -> Cancel:
        o.cancel_requested = True
        o.last_change = now
        return Cancel(o.tag)

    # ------------------------------------------------------------ protection
    def _ensure_protected(self, now: dt.datetime) -> list[Command]:
        """Invariant 3: every filled lot is covered by one broker-side SL-LMT."""
        qty = self.long_qty
        if qty <= 0 or self.state is PositionState.EXITING:
            return []
        p = self.protect
        if p is None:
            if self.working(*EXIT_PURPOSES):
                return []
            return [
                self._new_order(
                    now,
                    "PROTECT",
                    Side.SELL,
                    qty,
                    OrderType.SL_LMT,
                    self._sl_limit(),
                    self.backstop_trigger,
                )
            ]
        if p.remaining != qty and p.status is not OrderStatus.UNCERTAIN:
            p.qty = p.filled + qty
            p.last_change = now
            return [Modify(p.tag, qty=p.qty)]
        return []

    # ------------------------------------------------------------ timers
    def on_timer(self, now: dt.datetime) -> list[Command]:
        cmds: list[Command] = []
        c = self.cfg
        # entry: re-price toward the ask, within max slippage, then give up the rest
        for e in self.working("ENTRY"):
            if e.status in (OrderStatus.UNCERTAIN, OrderStatus.SENT) or e.cancel_requested:
                continue
            assert e.last_change is not None and e.first_limit is not None
            if (now - e.last_change).total_seconds() < c.reprice_after_s:
                continue
            new = self._buy_limit(self.last_quote) if self.last_quote else None
            cap = e.first_limit * (1 + Decimal(str(c.max_slippage_pct)) / 100)
            if e.reprices < c.max_reprices and new is not None and e.limit < new <= cap:
                e.reprices += 1
                e.limit, e.last_change = new, now
                cmds.append(Modify(e.tag, limit=new))
            else:
                cmds.append(self._cancel(now, e))  # keep any filled part (partial fill)
        # protection deadline (invariant 3)
        if (
            self.first_fill_at is not None
            and self.long_qty > 0
            and self.state is not PositionState.EXITING
        ):
            p = self.protect
            protected = p is not None and p.status is OrderStatus.TRIGGER_PENDING
            late = (now - self.first_fill_at).total_seconds() > c.protect_fill_within_s
            if not protected and late:
                cmds += [
                    Alert(
                        "critical",
                        f"{self.position_uid}: no protective stop "
                        f"{c.protect_fill_within_s:g}s after fill; "
                        "flattening",
                    ),
                    *self._flatten(now, "unprotected"),
                ]
                return cmds
        # gap-skipped stop (invariant 4)
        p = self.protect
        if p is not None and p.triggered_at is not None and p.remaining > 0:
            assert p.last_change is not None
            since = (now - max(p.triggered_at, p.last_change)).total_seconds()
            if since >= c.gap_chase_after_s:
                if p.chases < c.gap_chase_max_attempts:
                    p.chases += 1
                    q = self.last_quote
                    bid = q.bid if q and q.bid else p.limit
                    new = self._round(
                        min(
                            p.limit - c.gap_chase_step_ticks * self.tick,
                            bid - c.exit_buffer_ticks * self.tick,
                        ),
                        up=False,
                    )
                    p.limit, p.last_change = new, now
                    cmds.append(Modify(p.tag, limit=new))
                elif p.chases == c.gap_chase_max_attempts:
                    p.chases += 1
                    cmds.append(
                        Alert(
                            "critical",
                            f"{p.tag}: stop gapped and still unfilled "
                            f"after {c.gap_chase_max_attempts} chases",
                        )
                    )
        # exit: keep re-pricing toward the bid until flat
        for x in self.working("EXIT", "FLATTEN"):
            if x.status in (OrderStatus.UNCERTAIN, OrderStatus.SENT) or x.cancel_requested:
                continue
            assert x.last_change is not None
            if (now - x.last_change).total_seconds() < c.reprice_after_s:
                continue
            new = self._sell_limit(self.last_quote, x.limit)
            if new >= x.limit:
                new = self._round(x.limit - c.exit_buffer_ticks * self.tick, up=False)
            x.reprices += 1
            x.limit, x.last_change = new, now
            cmds.append(Modify(x.tag, limit=new))
            if x.reprices == c.flatten_max_attempts:
                cmds.append(
                    Alert("critical", f"{x.tag}: exit re-priced {x.reprices} times and still open")
                )
        return cmds


def check_invariants(m: PositionMachine) -> None:
    """Used by tests (and by the engine after every event) to catch a broken machine."""
    if m.long_qty < 0:
        raise InvariantViolation(f"net short: {m.long_qty}")  # 1
    open_sells = sum(o.remaining for o in m.working() if o.side is Side.SELL)
    if open_sells > m.long_qty:
        raise InvariantViolation(f"working sells {open_sells} > long {m.long_qty}")  # 1
    if len(m.working(*EXIT_PURPOSES)) > 1:
        raise InvariantViolation("two working exit-side orders")  # 2
    if len(m.orders) != len(set(m.orders)):
        raise InvariantViolation("duplicate tags")  # 5
