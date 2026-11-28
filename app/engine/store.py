"""Database side of trading: orders, order events, fills, positions and the ledger.

Implements the router's `OrderStore`. Writes go straight to SQLite (WAL); each call is one
short transaction, so a crash loses nothing that was already acknowledged. The database's
own constraints (one working exit per position, LMT/SL-LMT only, qty_open >= 0) are a
second line of defence under the state machine (D18).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.types import OrderStatus, Side
from app.db.models import Fill, Order, OrderEvent, Position, Trade
from app.engine.oms import Place, PositionMachine
from app.research.costs import ChargesTable


@dataclass(frozen=True)
class PositionMeta:
    instance_id: str
    book: str
    mode: str
    broker: str
    strategy_id: str | None
    signal_uid: str | None
    product: str
    stop_underlying: Decimal | None
    target_underlying: Decimal | None
    config_hash: str | None
    self_reported: bool = False


class DbOrderStore:
    def __init__(self, engine: Engine, now: Any) -> None:
        self.engine, self.now = engine, now
        self.meta: dict[str, PositionMeta] = {}
        self.keys: dict[str, str] = {}
        self._fills: dict[str, tuple[int, Decimal]] = {}  # tag -> (cum, avg)

    def open_position(
        self, m: PositionMachine, key_canon: str, meta: PositionMeta, at: dt.datetime
    ) -> None:
        self.meta[m.position_uid] = meta
        self.keys[m.position_uid] = key_canon
        with Session(self.engine) as s:
            s.add(
                Position(
                    position_uid=m.position_uid,
                    instance_id=meta.instance_id,
                    book=meta.book,
                    mode=meta.mode,
                    broker=meta.broker,
                    strategy_id=meta.strategy_id,
                    signal_uid=meta.signal_uid,
                    instrument_key=key_canon,
                    product=meta.product,
                    state=m.state.value,
                    qty_open=0,
                    strategy_stop_underlying=meta.stop_underlying,
                    target_underlying=meta.target_underlying,
                    backstop_sl_price=m.backstop_trigger,
                    opened_at=at,
                    self_reported=meta.self_reported,
                    config_hash=meta.config_hash,
                )
            )
            s.commit()

    # ------------------------------------------------------------ OrderStore protocol
    def save_new(self, m: PositionMachine, cmd: Place, key: Any) -> None:
        meta = self.meta[m.position_uid]
        now = self.now()
        with Session(self.engine) as s:
            if s.scalar(select(Order.id).where(Order.client_tag == cmd.tag)) is None:
                s.add(
                    Order(
                        client_tag=cmd.tag,
                        instance_id=meta.instance_id,
                        position_uid=m.position_uid,
                        book=meta.book,
                        mode=meta.mode,
                        broker=meta.broker,
                        purpose=cmd.purpose,
                        instrument_key=key.canonical,
                        side=cmd.side.value,
                        order_type=cmd.order_type.value,
                        product=meta.product,
                        qty=cmd.qty,
                        limit_price=cmd.limit,
                        trigger_price=cmd.trigger,
                        status=OrderStatus.NEW.value,
                        filled_qty=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
            s.add(
                OrderEvent(
                    client_tag=cmd.tag,
                    at=now,
                    from_status=None,
                    to_status=OrderStatus.NEW.value,
                    reason=f"{cmd.purpose} {cmd.side.value} {cmd.qty} "
                    f"{cmd.order_type.value} {cmd.limit} stored before send",
                )
            )
            s.commit()

    def save_status(
        self, tag: str, status: OrderStatus, filled: int, reason: str, purpose: str | None = None
    ) -> None:
        now = self.now()
        with Session(self.engine) as s:
            o = s.scalar(select(Order).where(Order.client_tag == tag))
            if o is None:
                return
            prev = o.status
            o.status, o.filled_qty, o.updated_at = status.value, min(filled, o.qty), now
            if purpose:
                o.purpose = purpose
            s.add(
                OrderEvent(
                    client_tag=tag, at=now, from_status=prev, to_status=status.value, reason=reason
                )
            )
            s.commit()

    def save_fill(self, tag: str, cum_filled: int, price: Decimal | None, at: dt.datetime) -> None:
        prev_cum, prev_avg = self._fills.get(tag, (0, Decimal(0)))
        inc = cum_filled - prev_cum
        if inc <= 0 or price is None:
            return
        inc_price = ((price * cum_filled) - (prev_avg * prev_cum)) / inc
        self._fills[tag] = (cum_filled, price)
        meta = None
        with Session(self.engine) as s:
            o = s.scalar(select(Order).where(Order.client_tag == tag))
            if o is not None:
                meta = self.meta.get(o.position_uid)
            s.add(
                Fill(
                    client_tag=tag,
                    broker_trade_id=f"{tag}-{cum_filled}",
                    at=at,
                    qty=inc,
                    price=inc_price.quantize(Decimal("0.01")),
                    self_reported=bool(meta and meta.self_reported),
                )
            )
            s.commit()

    def sync_position(self, m: PositionMachine) -> None:
        with Session(self.engine) as s:
            p = s.scalar(select(Position).where(Position.position_uid == m.position_uid))
            if p is None:
                return
            p.state, p.qty_open = m.state.value, m.long_qty
            buys = [o for o in m.orders.values() if o.side is Side.BUY and o.filled]
            q = sum(o.filled for o in buys)
            if q:
                total = sum(((o.avg_price or Decimal(0)) * o.filled for o in buys), Decimal(0))
                p.avg_entry_price = (total / q).quantize(Decimal("0.01"))
            if m.state.value == "CLOSED" and p.closed_at is None:
                p.closed_at = self.now()
            s.commit()

    def forget(self, m: PositionMachine) -> None:
        """Drop the in-memory caches of a settled position (bounded memory)."""
        self.meta.pop(m.position_uid, None)
        self.keys.pop(m.position_uid, None)
        for tag in m.orders:
            self._fills.pop(tag, None)

    # ------------------------------------------------------------ ledger
    def record_trade(
        self,
        m: PositionMachine,
        charges: ChargesTable,
        broker: str,
        overrides: list[dict[str, object]],
    ) -> Trade | None:
        buys = [o for o in m.orders.values() if o.side is Side.BUY and o.filled]
        sells = [o for o in m.orders.values() if o.side is Side.SELL and o.filled]
        bq, sq = sum(o.filled for o in buys), sum(o.filled for o in sells)
        if not bq or bq != sq:
            return None  # nothing traded, or not flat yet
        bv = sum(((o.avg_price or Decimal(0)) * o.filled for o in buys), Decimal(0))
        sv = sum(((o.avg_price or Decimal(0)) * o.filled for o in sells), Decimal(0))
        meta = self.meta[m.position_uid]
        entry_at = min(o.created_at for o in buys if o.created_at)
        exit_at = max(o.last_change for o in sells if o.last_change)
        ch = charges.option_round_trip(
            entry_at.date(),
            broker if broker in ("flattrade", "fyers") else "flattrade",
            bv,
            sv,
            orders=len(buys) + len(sells),
        )
        gross = sv - bv
        score = max(0, 100 - 25 * len(overrides))
        version = str(charges.version(entry_at.date())["version"])
        t = Trade(
            position_uid=m.position_uid,
            strategy_id=meta.strategy_id,
            book=meta.book,
            instrument_key=self.keys[m.position_uid],
            entry_at=entry_at,
            exit_at=exit_at,
            qty=bq,
            entry_price=(bv / bq).quantize(Decimal("0.01")),
            exit_price=(sv / sq).quantize(Decimal("0.01")),
            gross_pnl=gross,
            charges={k: str(v) for k, v in ch.items() if k != "total"},
            charges_total=ch["total"],
            net_pnl=gross - ch["total"],
            compliance_score=score,
            overrides=overrides,
            self_reported=meta.self_reported,
            charges_table_version=version,
            config_hash=meta.config_hash,
        )
        with Session(self.engine) as s:
            s.add(t)
            s.commit()
            s.refresh(t)
            s.expunge(t)
        return t
