"""The trading engine (brief sections 6, 9, 12, 13): one loop for PAPER, SEMI and LIVE.

Per completed 1-minute index candle:
  1. open positions: the strategy's `exit_logic` (trailing, its own exits)
  2. every strategy, in priority order, may emit signals
  3. each signal goes through the checks below and, if it passes, becomes a
     `PositionMachine` driven by an `OrderRouter` on the right broker

Books (proposal P1, D26):
  * `SHADOW:<strategy>` - unconstrained paper book per strategy: takes every one of its
    own signals that passes the time guards, data gate, contract and sizing checks. The
    strategy gate (40 paper trades etc.) is computed from it.
  * `PAPER` or `REAL` - the portfolio book that would really trade. Shared daily trade
    slots and concurrency limits, one position per underlying, breakers, cooldown; when
    two strategies want the same slot the lower priority one is `SUPERSEDED`.
    Its mode comes from `resolve_mode` (invariant 10); PAPER positions go to the paper
    broker, SEMI to the trader's task list, LIVE to the execution broker.

Safety rules enforced here:
  * inv 6  fail closed: no new entry on stale ticks/chain, a candle gap, unmapped
           instrument, expired token, clock drift or unfinished warm-up
  * inv 7  restart: reconcile with the broker, protect or flatten anything unmanaged
  * inv 8  breakers per book, persistent
  * inv 9  AI is advisory only: nothing here waits for it (P2.5 adds a side channel)
  * inv 11 flatten-all works from any state and stops new entries
  * inv 13 time guards and the 15:10 square-off
  * inv 14 a LIVE strategy that loses LIVE mid-session takes no new entries (alert)
  * inv 16 foreign orders in the book stop new entries
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.adapters.base import BaseExecutionBroker, InstrumentInfo, TickData
from app.adapters.fyers.symbols import SymbolMap
from app.brokers.paper import Depth, PaperBroker
from app.brokers.semi import SemiBroker, semi_mechanics
from app.config.schema import PlatformConfig, StrategyEntry
from app.core.timeutil import to_ist
from app.core.types import (
    GateStatus,
    InstrumentKey,
    Mode,
    PositionState,
    Product,
)
from app.data.bars import Bar
from app.data.candle_builder import CandleBuilder
from app.db.models import Position as PositionRow
from app.db.models import SignalRow, StrategyRow
from app.engine.alerts import AlertBus
from app.engine.arming import Arming
from app.engine.contracts import (
    backstop_trigger,
    choose_contract,
    estimate_delta,
    liquidity_block,
)
from app.engine.instance import foreign_orders, heartbeat
from app.engine.modes import ModeInputs, live_loss_action, resolve_mode
from app.engine.oms import Alert, PositionMachine, Quote
from app.engine.reconcile import TagSource, reconcile, used_tags
from app.engine.risk import (
    Breakers,
    DataHealth,
    data_block_reasons,
    entry_block_reason,
    must_square_off,
)
from app.engine.router import OrderRouter
from app.engine.sizing import size_lots
from app.engine.store import DbOrderStore, PositionMeta
from app.research.costs import ChargesTable
from app.strategies.base import (
    MarketView,
    OIPulse,
    OpenPosition,
    Signal,
    StrategyPlugin,
)

log = logging.getLogger(__name__)
M = dt.timedelta(minutes=1)
LONG = ("LONG_CE", "LONG")


# ---------------------------------------------------------------- inputs
@dataclass
class Health:
    """Set by the runtime (login, NTP, static-IP checks); read on every entry."""

    data_login: bool = False  # today's data-broker login is valid
    exec_login: bool = False  # execution-broker login is valid
    static_ip_ok: bool = False
    clock_drift_ms: float | None = None
    instruments_mapped: bool = True


class History(Protocol):
    def prior_day(self, underlying: str, d: dt.date) -> Bar | None: ...
    def oi_pulse(self, underlying: str, now: dt.datetime) -> OIPulse | None: ...
    def chain_at(self, underlying: str) -> dt.datetime | None: ...
    def roll_reset(self, underlying: str, d: dt.date) -> bool: ...


class NoHistory:
    def prior_day(self, underlying: str, d: dt.date) -> Bar | None:
        return None

    def oi_pulse(self, underlying: str, now: dt.datetime) -> OIPulse | None:
        return None

    def chain_at(self, underlying: str) -> dt.datetime | None:
        return None

    def roll_reset(self, underlying: str, d: dt.date) -> bool:
        return False


QuoteFetch = Callable[[InstrumentKey], Awaitable[Depth | None]]


@dataclass
class Slot:
    """One strategy plugin as the engine runs it."""

    id: str
    plugin: StrategyPlugin
    entry: StrategyEntry
    warm_1m: int  # 1-minute bars needed before signals count (invariant 6)
    seen: int = 0
    was_live: bool = False
    live_lost: bool = False


@dataclass
class LivePos:
    uid: str
    book: str
    mode: Mode
    slot: Slot
    signal: Signal
    key: InstrumentKey
    router: OrderRouter
    machine: PositionMachine
    stop: float
    target: float | None
    entry_underlying: float
    initial_risk: float
    opened_at: dt.datetime
    product: Product = Product.MIS
    exit_sent: bool = False

    @property
    def long(self) -> bool:
        return self.signal.direction in LONG


@dataclass
class Decision:
    book: str
    outcome: str  # OPENED / BLOCKED / SUPERSEDED / SKIPPED_* ...
    detail: str = ""


@dataclass
class EngineState:
    halted: str | None = None  # entries stopped (flatten-all, foreign orders, ...)
    signals: int = 0
    decisions: list[Decision] = field(default_factory=list)


def shadow_book(sid: str) -> str:
    return f"SHADOW:{sid}"


# ---------------------------------------------------------------- the engine
class TradingEngine:
    def __init__(
        self,
        cfg: PlatformConfig,
        db: Engine,
        smap: SymbolMap,
        now: Callable[[], dt.datetime],
        slots: list[Slot],
        alerts: AlertBus,
        charges: ChargesTable,
        process_id: str,
        paper_factory: Callable[[str, dict[InstrumentKey, InstrumentInfo]], PaperBroker],
        semi: SemiBroker | None = None,
        live: BaseExecutionBroker | None = None,
        history: History | None = None,
        quote_fetch: QuoteFetch | None = None,
        subscribe: Callable[[InstrumentKey], None] | None = None,
        health: Health | None = None,
        on_signal: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self.cfg, self.db, self.smap, self.now = cfg, db, smap, now
        self.slots = slots
        self.alerts, self.charges, self.process_id = alerts, charges, process_id
        self.paper_factory, self.semi, self.live = paper_factory, semi, live
        self.history: History = history or NoHistory()
        self.quote_fetch, self.subscribe = quote_fetch, subscribe
        self.health = health or Health()
        self.on_signal = on_signal  # AI advisory side channel: fire and forget (inv 9)
        self.iid = cfg.instance.instance_id
        self.state = EngineState()
        self.store = DbOrderStore(db, now)
        self.arming = Arming(db, self.iid, process_id, cfg.time_guards.session_close)
        self.instruments: dict[InstrumentKey, InstrumentInfo] = {}
        self.paper: dict[str, PaperBroker] = {}
        self.routers: dict[str, OrderRouter] = {}
        self.positions: dict[str, LivePos] = {}
        self.breakers: dict[str, Breakers] = {}
        self.depth: dict[InstrumentKey, Depth] = {}
        self.vix: float | None = None
        self.spot: dict[str, float] = {}
        self.last_tick_at: dict[str, dt.datetime] = {}
        self.builders: dict[str, CandleBuilder] = {}
        self.fut_builders: dict[str, CandleBuilder] = {}
        self.fut_bars: dict[str, dict[dt.datetime, Bar]] = {}
        self.last_bar: dict[str, Bar] = {}
        self.last_exit: dict[str, dt.datetime] = {}
        self.taken_this_candle: dict[tuple[str, dt.datetime], str] = {}
        self._last_foreign_check: dt.datetime | None = None
        self._last_heartbeat: dt.datetime | None = None
        self.tags = TagSource(self.iid, to_ist(now()).date())

    # ------------------------------------------------------------ brokers and books
    def _info(self, key: InstrumentKey) -> InstrumentInfo:
        info = self.instruments.get(key)
        if info is None:
            info = self.smap.info(key)
            self.instruments[key] = info
        return info

    def _paper_for(self, book: str) -> PaperBroker:
        b = self.paper.get(book)
        if b is None:
            b = self.paper_factory(book, self.instruments)
            self.paper[book] = b
            for k, d in self.depth.items():  # a new book sees the market it joins
                b.on_market(k, d)
        return b

    def _router(self, book: str, mode: Mode) -> OrderRouter:
        name = f"paper:{book}" if mode is Mode.PAPER else mode.value.lower()
        r = self.routers.get(name)
        if r is None:
            broker: BaseExecutionBroker
            if mode is Mode.PAPER:
                broker = self._paper_for(book)
            elif mode is Mode.SEMI:
                assert self.semi is not None
                broker = self.semi
            else:
                assert self.live is not None
                broker = self.live
            r = OrderRouter(broker, self.store, self.now, self._on_machine_alert)
            self.routers[name] = r
        return r

    def _breakers(self, book: str) -> Breakers:
        b = self.breakers.get(book)
        if b is None:
            b = Breakers(self.db, self.cfg.risk, book, self.iid)
            self.breakers[book] = b
        return b

    def _on_machine_alert(self, a: Alert) -> None:
        self.alerts.emit(a.level, "ORDER", a.message)

    # ------------------------------------------------------------ start-up
    async def start(self) -> None:
        """Reconcile every paper book on disk (and the live broker) before trading."""
        now = self.now()
        books = sorted({b for b in self._stored_paper_books()})
        all_tags: list[str] = []
        brokers: list[BaseExecutionBroker] = [self._paper_for(b) for b in books]
        if self.live is not None:
            brokers.append(self.live)
        for broker in brokers:
            for o in await broker.order_book():
                if o.client_tag:
                    all_tags.append(o.client_tag)
                if o.key is not None and o.key.tradable:
                    self._info(o.key)  # instruments the books already hold
        self.tags = TagSource(self.iid, to_ist(now).date(), used_tags(self.db, []) + all_tags)
        for book in books:
            await self._reconcile(book, Mode.PAPER)
        if self.live is not None:
            await self._reconcile("REAL", Mode.LIVE)
        if self._stored_open(Mode.SEMI):
            self.alerts.emit(
                "critical",
                "RECONCILE",
                "SEMI positions were open when the engine stopped: check your broker app "
                "and report them on the dashboard; the engine cannot see that account",
            )
        self._heartbeat(now)

    def _stored_paper_books(self) -> list[str]:
        with Session(self.db) as s:
            rows = s.scalars(
                select(PositionRow.book).where(
                    PositionRow.instance_id == self.iid, PositionRow.mode == Mode.PAPER.value
                )
            )
            return list(set(rows))

    def _stored_open(self, mode: Mode) -> bool:
        with Session(self.db) as s:
            return (
                s.scalar(
                    select(PositionRow.id).where(
                        PositionRow.instance_id == self.iid,
                        PositionRow.mode == mode.value,
                        PositionRow.state.notin_(("CLOSED", "REJECTED", "SKIPPED_SIZE")),
                    )
                )
                is not None
            )

    async def _reconcile(self, book: str, mode: Mode) -> None:
        router = self._router(book, mode)
        now = self.now()
        rep = reconcile(
            self.db,
            self.iid,
            await router.broker.order_book(),
            await router.broker.positions(),
            now,
            lambda: f"R{uuid.uuid4().hex[:11]}",
            book=book,
            mode=mode.value,
        )
        for n in rep.notes:
            self.alerts.emit("warn", "RECONCILE", f"{book}: {n}")
        if rep.foreign:
            self._halt(f"{len(rep.foreign)} order(s) from another instance in the {book} book")
        for o in rep.orphan_orders:
            await router.broker.cancel_order(o.broker_order_id)
        for r in rep.recovered:
            info = self._info(r.key)
            backstop = r.backstop or backstop_trigger(
                r.broker_avg or Decimal(1),
                0.0,
                info.tick_size,
                "pct",
                1.0,
                self.cfg.contracts.backstop_sl_pct_of_premium,
            )
            m = PositionMachine.adopt(
                r.position_uid,
                info.lot_size,
                info.tick_size,
                self.cfg.execution,
                self.tags,
                now,
                r.orders,
                r.broker_long,
                r.broker_avg,
                backstop,
            )
            if not r.known:
                self.store.open_position(
                    m,
                    r.key.canonical,
                    PositionMeta(
                        self.iid,
                        book,
                        mode.value,
                        router.broker.name,
                        None,
                        None,
                        Product.MIS.value,
                        None,
                        None,
                        None,
                    ),
                    now,
                )
            else:
                self.store.meta[m.position_uid] = PositionMeta(
                    self.iid,
                    r.book or book,
                    mode.value,
                    router.broker.name,
                    r.strategy_id,
                    None,
                    Product.MIS.value,
                    None,
                    None,
                    None,
                )
                self.store.keys[m.position_uid] = r.key.canonical
            for ro in r.orders:  # the database now agrees with the broker
                self.store.save_status(ro.tag, ro.status, ro.filled, "reconciled at restart")
            router.adopt(m, r.key, Product.MIS, r.oids)
            if m.state is PositionState.UNMANAGED:
                self.alerts.emit(
                    "critical",
                    "RECONCILE",
                    f"{book}: {r.key.canonical} x{r.broker_long} held with no working stop "
                    f"after restart: {self.cfg.execution.unmanaged_action}",
                )
                await router.execute(
                    m.position_uid, m.take_over(now, self.cfg.execution.unmanaged_action)
                )
            elif m.state is not PositionState.CLOSED:
                self.alerts.emit(
                    "warn", "RECONCILE", f"{book}: resumed {r.key.canonical} ({m.state.value})"
                )
            self.store.sync_position(m)
            slot = next((s for s in self.slots if s.id == r.strategy_id), None)
            if slot is not None and m.state is not PositionState.CLOSED and r.known:
                stop, target = self._stored_levels(r.position_uid)
                self.positions[m.position_uid] = LivePos(
                    m.position_uid,
                    r.book or book,
                    mode,
                    slot,
                    Signal(
                        strategy_id=slot.id,
                        underlying=r.key.underlying or "",
                        direction="LONG_CE"
                        if r.key.option_type and r.key.option_type.value == "CE"
                        else "LONG_PE",
                        at=now,
                        reference_price=Decimal(str(stop)),
                        stop_underlying=Decimal(str(stop)),
                        target_underlying=None if target is None else Decimal(str(target)),
                        checklist={"restored": True},
                        config_hash="restored",
                    ),
                    r.key,
                    router,
                    m,
                    stop,
                    target,
                    stop,
                    0.0,
                    now,
                )

    def _stored_levels(self, puid: str) -> tuple[float, float | None]:
        with Session(self.db) as s:
            p = s.scalar(select(PositionRow).where(PositionRow.position_uid == puid))
        if p is None or p.strategy_stop_underlying is None:
            return 0.0, None
        t = p.target_underlying
        return float(p.strategy_stop_underlying), None if t is None else float(t)

    # ------------------------------------------------------------ warm-up
    def warm_up(self, view: MarketView) -> None:
        """Feed one historical candle to every plugin; signals are discarded."""
        for s in self.slots:
            s.plugin.on_candle("1m", view)
            s.seen += 1
        self.last_bar[view.underlying] = view.bar

    # ------------------------------------------------------------ market data
    async def on_tick(self, t: TickData) -> None:
        k = t.key
        now = self.now()
        if k.kind == "INDEX":
            u = k.underlying or ""
            if u == "INDIAVIX":
                self.vix = float(t.ltp)
                return
            self.last_tick_at[u] = now
            self.spot[u] = float(t.ltp)
            b = self.builders.setdefault(u, CandleBuilder(self.cfg.freshness.candle_close_grace_s))
            for bar in b.add(t):
                await self.on_bar(u, bar)
            await self._check_levels(u, float(t.ltp), float(t.ltp))
        elif k.kind in ("FUT", "FUTC"):
            u = k.underlying or ""
            fb = self.fut_builders.setdefault(
                u, CandleBuilder(self.cfg.freshness.candle_close_grace_s)
            )
            for bar in fb.add(t):
                self.fut_bars.setdefault(u, {})[bar.ts] = bar
        elif k.kind == "OPT":
            d = Depth(t.bid, t.ask, t.bid_qty or 0, t.ask_qty or 0, t.ltp, now)
            await self.on_option_depth(k, d)

    async def on_option_depth(self, k: InstrumentKey, d: Depth) -> None:
        self.depth[k] = d
        for b in self.paper.values():
            b.on_market(k, d)
        q = Quote(d.bid, d.ask, d.ltp, d.at)
        for p in self.positions.values():
            if p.key == k:
                p.machine.on_quote(d.at, q)
        for r in self.routers.values():
            await r.pump()

    async def on_timer(self) -> None:
        """Call about once a second: closes candles, runs order timers, square-off,
        settles closed trades, checks foreign orders and writes the heartbeat."""
        now = self.now()
        for u, b in list(self.builders.items()):
            for bar in b.close_until(now):
                await self.on_bar(u, bar)
        for u, fb in self.fut_builders.items():
            for bar in fb.close_until(now):
                self.fut_bars.setdefault(u, {})[bar.ts] = bar
        for pb in self.paper.values():
            pb.advance()
        if self.semi is not None:
            self.semi.tick()
        for r in list(self.routers.values()):
            await r.tick()
        await self._square_off(now)
        self._settle(now)
        await self._foreign_check(now)
        self._heartbeat(now)

    # ------------------------------------------------------------ candles
    async def on_bar(self, u: str, bar: Bar) -> None:
        now = bar.ts + M
        d = to_ist(bar.ts).date()
        prev = self.last_bar.get(u)
        new_session = prev is None or to_ist(prev.ts).date() != d
        gap = not new_session and prev is not None and bar.ts - prev.ts != M
        self.last_bar[u] = bar
        fut = self.fut_bars.get(u, {}).pop(bar.ts, None)
        view = MarketView(
            underlying=u,
            now=now,
            bar=bar,
            futures_bar=fut,
            prior_day=self.history.prior_day(u, d),
            oi_pulse=self.history.oi_pulse(u, now),
            volume_series_reset=new_session and self.history.roll_reset(u, d),
            new_session=new_session,
        )
        # 1) strategy exits on the completed candle
        for p in [p for p in self.positions.values() if p.signal.underlying == u]:
            if p.exit_sent:
                continue
            dec = p.slot.plugin.exit_logic(
                OpenPosition(
                    p.signal.direction,
                    p.entry_underlying,
                    p.stop,
                    p.target,
                    p.initial_risk,
                    p.opened_at,
                ),
                view,
            )
            if dec.action == "TRAIL" and dec.new_stop_underlying is not None:
                p.stop = float(dec.new_stop_underlying)
            elif dec.action in ("EXIT_TARGET", "EXIT_STOP"):
                await self._exit(p, dec.reason or dec.action.lower())
        await self._check_levels(u, bar.low, bar.high)
        # 2) signals, in priority order
        order = self._priorities()
        cands: list[tuple[Slot, Signal]] = []
        for s in sorted(self.slots, key=lambda s: order.get(s.id, 999)):
            sigs = s.plugin.on_candle("1m", view)
            s.seen += 1
            cands += [(s, g) for g in sigs]
        for s, g in cands:
            await self._consider(s, g, view, gap)

    def _priorities(self) -> dict[str, int]:
        with Session(self.db) as s:
            return {r.id: r.priority for r in s.scalars(select(StrategyRow))}

    def _row(self, sid: str) -> StrategyRow | None:
        with Session(self.db) as s:
            return s.get(StrategyRow, sid)

    # ------------------------------------------------------------ entries
    async def _consider(self, slot: Slot, sig: Signal, view: MarketView, gap: bool) -> None:
        now = self.now()
        self.state.signals += 1
        uid = uuid.uuid4().hex[:16]
        u = sig.underlying
        checklist: dict[str, object] = dict(sig.checklist)
        row = self._row(slot.id)
        common: list[str] = []
        if row is None or not row.active:
            common.append("strategy switched off")
        if u not in self.cfg.risk.tradable_underlyings:
            common.append(f"{u} is research-only (risk.tradable_underlyings)")
        if (tb := entry_block_reason(now, self.cfg.time_guards)) is not None:
            common.append(tb)
        needs = slot.plugin.data_needs(slot.plugin.params)
        login_ok = self.health.data_login
        health = DataHealth(
            now=now,
            last_tick_at=self.last_tick_at.get(u),
            chain_at=self.history.chain_at(u),
            chain_needed=needs.needs_option_chain,
            candle_gap=gap,
            instruments_mapped=self.health.instruments_mapped,
            token_valid=login_ok,
            clock_drift_ms=self.health.clock_drift_ms,
            warmed_up=slot.seen >= slot.warm_1m,
        )
        common += data_block_reasons(
            health, self.cfg.freshness, self.cfg.instance.max_clock_drift_ms
        )
        if sig.direction not in ("LONG_CE", "LONG_PE"):
            common.append("only option-buying signals are traded")
        if common:
            self._record(uid, slot, sig, checklist, "BLOCKED: " + "; ".join(common))
            return
        # contract, quote, liquidity, size: computed once, shared by both books
        spot = float(sig.reference_price)
        choice = choose_contract(
            self.smap, u, sig.direction, spot, now, to_ist(now).date(), self.cfg.contracts
        )
        checklist["contract"] = choice.key.canonical if choice.key else choice.reason
        if choice.key is None:
            self._record(uid, slot, sig, checklist, f"BLOCKED: {choice.reason}")
            return
        key = choice.key
        info = self._info(key)
        if self.subscribe is not None:
            self.subscribe(key)
        depth = self.depth.get(key)
        if (depth is None or (now - depth.at).total_seconds() > 5) and self.quote_fetch:
            depth = await self.quote_fetch(key)
            if depth is not None:
                await self.on_option_depth(key, depth)
        liq = liquidity_block(depth, info.lot_size, self.cfg.contracts)
        if liq is not None or depth is None or depth.ask is None:
            self._record(uid, slot, sig, checklist, f"BLOCKED: {liq or 'no quote'}")
            return
        delta, dsrc = estimate_delta(key, spot, now, self.vix, None)
        idx_stop = abs(spot - float(sig.stop_underlying))
        prem_stop = idx_stop * delta
        c = self.cfg.contracts
        trigger = backstop_trigger(
            depth.ask,
            prem_stop,
            info.tick_size,
            c.backstop_mode,
            c.backstop_delta_multiple,
            c.backstop_sl_pct_of_premium,
        )
        bs_mult = float(depth.ask - trigger) / prem_stop if prem_stop > 0 else 99.0
        r = self.cfg.risk
        size = size_lots(
            risk_inr=float(r.risk_per_trade_inr),
            stop_dist=prem_stop,
            premium=float(depth.ask),
            lot=info.lot_size,
            capital=float(r.capital_inr),
            max_premium_pct=r.max_premium_pct_of_capital,
            max_lots=r.max_lots,
            backstop_multiple=bs_mult,
            max_backstop_loss_multiple=r.max_backstop_loss_multiple_of_risk,
            freeze_qty=info.freeze_qty,
        )
        checklist.update(
            {
                "delta": round(delta, 3),
                "delta_source": dsrc,
                "premium_stop": round(prem_stop, 2),
                "backstop_trigger": str(trigger),
                "lots": size.lots,
            }
        )
        if size.skip_reason:
            self._record(uid, slot, sig, checklist, size.skip_reason)
            return
        opened = False
        # shadow book: unconstrained except one position per underlying in that book
        sb = shadow_book(slot.id)
        if self._open_on(sb, u):
            checklist["shadow"] = "POSITION_OPEN"
        else:
            await self._open(sb, Mode.PAPER, slot, sig, key, depth, size.lots, trigger, uid)
            checklist["shadow"] = "OPENED"
            opened = True
        # portfolio book
        mode, why = self._mode_for(slot, row)
        book = "PAPER" if mode is Mode.PAPER else "REAL"
        checklist["mode"] = mode.value
        if why:
            checklist["mode_reasons"] = why
        block = self._portfolio_block(slot, book, u, now)
        if block is None:
            await self._open(book, mode, slot, sig, key, depth, size.lots, trigger, uid)
            outcome = f"OPENED {book} {mode.value}"
            opened = True
        else:
            outcome = block
        if opened:
            slot.plugin.on_trade_opened(sig)
        self._record(uid, slot, sig, checklist, outcome)

    def _open_on(self, book: str, u: str) -> bool:
        return any(p.book == book and p.signal.underlying == u for p in self.positions.values())

    def _portfolio_block(self, slot: Slot, book: str, u: str, now: dt.datetime) -> str | None:
        r = self.cfg.risk
        if self.state.halted:
            return f"BLOCKED: entries halted ({self.state.halted})"
        if slot.live_lost:
            return "BLOCKED: LIVE lost this session (invariant 14)"
        key = (book, self.last_bar[u].ts if u in self.last_bar else now)
        if key in self.taken_this_candle and self.taken_this_candle[key] != slot.id:
            return f"SUPERSEDED by {self.taken_this_candle[key]}"
        mine = [p for p in self.positions.values() if p.book == book]
        on_u = [p for p in mine if p.signal.underlying == u]
        if on_u:
            other = on_u[0].slot.id
            return "BLOCKED: position open" if other == slot.id else f"SUPERSEDED by {other}"
        if len(mine) >= r.max_concurrent_positions:
            return f"SUPERSEDED: max {r.max_concurrent_positions} concurrent position(s)"
        last = self.last_exit.get(book)
        if last is not None and now - last < dt.timedelta(minutes=r.cooldown_minutes_after_exit):
            return "BLOCKED: cooldown after exit"
        br = self._breakers(book).check(now, self._unrealized(book))
        if br.tripped:
            return "BLOCKED: breaker: " + "; ".join(br.reasons)
        self.taken_this_candle[key] = slot.id
        return None

    def _mode_for(self, slot: Slot, row: StrategyRow | None) -> tuple[Mode, tuple[str, ...]]:
        now = self.now()
        req = Mode(row.requested_mode) if row else Mode.PAPER
        gate = GateStatus(row.gate_status) if row else GateStatus.NOT_RUN
        data_ok = self.health.data_login
        exec_ok = self.health.exec_login and self.live is not None
        res = resolve_mode(
            ModeInputs(
                requested=req,
                ceiling=self.cfg.modes.global_ceiling,
                allow_semi=slot.entry.allow_semi,
                gate=gate,
                armed_today=self.arming.is_armed(now),
                data_token_ok=data_ok,
                role=self.cfg.instance.instance_role,
                static_ip_ok=self.health.static_ip_ok,
                exec_token_ok=exec_ok,
            )
        )
        mode, reasons = res.mode, res.reasons
        if mode is Mode.SEMI and self.semi is None:
            mode, reasons = Mode.PAPER, (*reasons, "no SEMI task channel running")
        if mode is Mode.LIVE:
            slot.was_live = True
        elif slot.was_live and not slot.live_lost:
            action = live_loss_action(
                slot.plugin.segment.value == "options", slot.entry.semi_fallback
            )
            if action == "no_new_entries":
                slot.live_lost = True
                self.alerts.emit(
                    "critical",
                    "MODE",
                    f"{slot.id}: LIVE lost mid-session ({'; '.join(reasons)}). No new entries; "
                    "open positions stay protected by their broker-side stops",
                )
        return mode, reasons

    async def _open(
        self,
        book: str,
        mode: Mode,
        slot: Slot,
        sig: Signal,
        key: InstrumentKey,
        depth: Depth,
        lots: int,
        trigger: Decimal,
        signal_uid: str,
    ) -> None:
        now = self.now()
        info = self._info(key)
        router = self._router(book, mode)
        mech = semi_mechanics(self.cfg.execution) if mode is Mode.SEMI else self.cfg.execution
        puid = f"P{uuid.uuid4().hex[:15]}"
        m = PositionMachine(
            puid, info.lot_size, info.tick_size, lots * info.lot_size, trigger, mech, self.tags
        )
        meta = PositionMeta(
            self.iid,
            book,
            mode.value,
            router.broker.name,
            slot.id,
            signal_uid,
            Product.MIS.value,
            sig.stop_underlying,
            sig.target_underlying,
            sig.config_hash,
            self_reported=mode is Mode.SEMI,
        )
        self.store.open_position(m, key.canonical, meta, now)
        router.track(m, key, Product.MIS)
        entry_px = float(sig.reference_price)
        stop = float(sig.stop_underlying)
        target = None if sig.target_underlying is None else float(sig.target_underlying)
        self.positions[puid] = LivePos(
            puid,
            book,
            mode,
            slot,
            sig,
            key,
            router,
            m,
            stop,
            target,
            entry_px,
            abs(entry_px - stop),
            now,
        )
        cmds = m.start_entry(now, Quote(depth.bid, depth.ask, depth.ltp, depth.at))
        if mode is Mode.SEMI and self.semi is not None and cmds:
            first = cmds[0]
            self.semi.plan_stop(getattr(first, "tag", ""), trigger, m._sl_limit())
        if not book.startswith("SHADOW:"):
            self._breakers(book).record_entry(now)
        await router.execute(puid, cmds)
        self.alerts.emit(
            "info",
            "TRADE",
            f"{book} {mode.value}: {slot.id} {sig.direction} {key.canonical} "
            f"x{lots} lot(s), stop {stop:.2f}",
        )

    def _record(
        self, uid: str, slot: Slot, sig: Signal, checklist: dict[str, object], outcome: str
    ) -> None:
        self.state.decisions.append(Decision(slot.id, outcome))
        if len(self.state.decisions) > 500:
            del self.state.decisions[:100]
        with Session(self.db) as s:
            s.add(
                SignalRow(
                    signal_uid=uid,
                    strategy_id=slot.id,
                    config_hash=sig.config_hash,
                    instance_id=self.iid,
                    created_at=self.now(),
                    underlying=sig.underlying,
                    direction=sig.direction,
                    inputs={
                        "reference": str(sig.reference_price),
                        "stop": str(sig.stop_underlying),
                        "target": None
                        if sig.target_underlying is None
                        else str(sig.target_underlying),
                        "signal_at": sig.at.isoformat(),
                    },
                    checklist=_jsonable(checklist),
                    outcome=outcome[:200],
                )
            )
            s.commit()
        if self.on_signal is not None:
            snap: dict[str, object] = {
                "strategy": slot.id,
                "underlying": sig.underlying,
                "direction": sig.direction,
                "at_ist": to_ist(sig.at).isoformat(),
                "reference": float(sig.reference_price),
                "stop": float(sig.stop_underlying),
                "target": None if sig.target_underlying is None else float(sig.target_underlying),
                "india_vix": self.vix,
                "checklist": _jsonable(checklist),
                "outcome": outcome,
            }
            try:
                self.on_signal(uid, snap)  # only schedules a task; never awaited here
            except Exception:
                log.exception("AI hook failed (ignored: advisory only)")

    # ------------------------------------------------------------ exits
    async def _check_levels(self, u: str, low: float, high: float) -> None:
        for p in [p for p in self.positions.values() if p.signal.underlying == u]:
            if p.exit_sent:
                continue
            if p.long:
                if low <= p.stop:
                    await self._exit(p, "strategy stop")
                elif p.target is not None and high >= p.target:
                    await self._exit(p, "target")
            else:
                if high >= p.stop:
                    await self._exit(p, "strategy stop")
                elif p.target is not None and low <= p.target:
                    await self._exit(p, "target")

    async def _exit(self, p: LivePos, reason: str) -> None:
        p.exit_sent = True
        await p.router.execute(p.uid, p.machine.request_exit(self.now(), reason))

    async def _square_off(self, now: dt.datetime) -> None:
        for p in list(self.positions.values()):
            if not p.exit_sent and must_square_off(now, self.cfg.time_guards, p.product):
                await self._exit(p, "square-off 15:10")

    async def flatten_all(self, reason: str, by: str) -> int:
        """Invariant 11: exit everything, from any state, and stop new entries."""
        self._halt(f"flatten-all by {by}: {reason}")
        n = 0
        for r in self.routers.values():
            for t in list(r.positions.values()):
                m = t.machine
                if m.state in (PositionState.CLOSED, PositionState.REJECTED):
                    continue
                await r.execute(m.position_uid, m.request_exit(self.now(), "flatten"))
                n += 1
        for p in self.positions.values():
            p.exit_sent = True
        return n

    def _halt(self, why: str) -> None:
        if self.state.halted is None:
            self.state.halted = why
            self.alerts.emit("critical", "RISK", f"new entries stopped: {why}")

    def resume(self, by: str) -> None:
        if self.state.halted:
            self.alerts.emit("warn", "RISK", f"entries resumed by {by} (was: {self.state.halted})")
        self.state.halted = None

    # ------------------------------------------------------------ settlement
    def _settle(self, now: dt.datetime) -> None:
        for r in self.routers.values():
            for m in r.untrack_closed():
                p = self.positions.pop(m.position_uid, None)
                book = p.book if p else self.store.meta[m.position_uid].book
                broker = self.cfg.research.execution_broker
                t = self.store.record_trade(m, self.charges, broker, [])
                if t is None:
                    continue
                self._breakers(book).record_close(now, t.net_pnl)
                self.last_exit[book] = now
                self.alerts.emit(
                    "info",
                    "TRADE",
                    f"{book}: closed {t.instrument_key} net {t.net_pnl:+.2f} "
                    f"(gross {t.gross_pnl:+.2f}, charges {t.charges_total:.2f})",
                )
        # unrealized P&L can trip a breaker while a trade is open
        for book in {p.book for p in self.positions.values() if not p.book.startswith("SHADOW")}:
            self._breakers(book).check(now, self._unrealized(book))

    def _unrealized(self, book: str) -> Decimal:
        tot = Decimal(0)
        for p in self.positions.values():
            if p.book != book or p.machine.long_qty <= 0:
                continue
            d = self.depth.get(p.key)
            buys = [o for o in p.machine.orders.values() if o.side.value == "BUY" and o.filled]
            q = sum(o.filled for o in buys)
            if d is None or d.bid is None or not q:
                continue
            avg = sum(((o.avg_price or Decimal(0)) * o.filled for o in buys), Decimal(0)) / q
            tot += (d.bid - avg) * p.machine.long_qty
        return tot

    # ------------------------------------------------------------ housekeeping
    async def _foreign_check(self, now: dt.datetime) -> None:
        every = self.cfg.execution.foreign_instance_check_s
        if self._last_foreign_check and (now - self._last_foreign_check).total_seconds() < every:
            return
        self._last_foreign_check = now
        brokers: list[BaseExecutionBroker] = list(self.paper.values())
        if self.live is not None:
            brokers.append(self.live)
        for b in brokers:
            try:
                book = await b.order_book()
            except Exception as e:  # a failed check is itself a reason to stop entries
                self._halt(f"order book check failed on {b.name}: {type(e).__name__}")
                continue
            f = foreign_orders(book, self.iid)
            if f:
                self._halt(
                    f"{len(f)} order(s) from another instance on {b.name}: "
                    + ", ".join(o.client_tag or "?" for o in f[:3])
                )

    def _heartbeat(self, now: dt.datetime) -> None:
        if self._last_heartbeat and (now - self._last_heartbeat).total_seconds() < 5:
            return
        self._last_heartbeat = now
        heartbeat(
            self.db,
            self.iid,
            self.cfg.instance.instance_role.value,
            self.cfg.instance.host_label,
            self.cfg.instance.registered_static_ip,
            self.health.static_ip_ok,
            self.process_id,
            now,
        )

    def is_exposed(self, sid: str) -> bool:
        """Invariant 17: open positions or working orders for this strategy (any book
        except its shadow book, which follows the strategy automatically)."""
        return any(
            p.slot.id == sid and not p.book.startswith("SHADOW:") for p in self.positions.values()
        )

    def snapshot(self) -> dict[str, object]:
        """Plain data for the dashboard / Telegram status."""
        return {
            "halted": self.state.halted,
            "signals": self.state.signals,
            "armed": self.arming.is_armed(self.now()),
            "positions": [
                {
                    "uid": p.uid,
                    "book": p.book,
                    "mode": p.mode.value,
                    "strategy": p.slot.id,
                    "key": p.key.canonical,
                    "state": p.machine.state.value,
                    "qty": p.machine.long_qty,
                    "stop": p.stop,
                    "target": p.target,
                }
                for p in self.positions.values()
            ],
        }


def _jsonable(d: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, v in d.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, dict):
            out[k] = _jsonable({str(a): b for a, b in v.items()})
        elif isinstance(v, (list, tuple)):
            out[k] = [
                x if isinstance(x, (str, int, float, bool)) or x is None else str(x) for x in v
            ]
        else:
            out[k] = str(v)
    return out
