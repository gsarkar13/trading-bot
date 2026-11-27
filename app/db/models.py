"""Database schema (brief section 19).

Conventions:
  * every timestamp is UTC (`UTCDateTime`), displayed in IST by the UI
  * every price / money value is an exact decimal (`ExactDecimal`), never a float
  * `book` separates PAPER, real-money and shadow-variant results (section 5.3)
  * tokens are never stored in plain text; `login_sessions` holds metadata only

Phase 0 creates the tables. Later phases add behaviour, not new core columns, wherever
possible; any change goes through an Alembic migration.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.types import EXIT_PURPOSES, WORKING_ORDER_STATUSES, OrderStatus
from app.db.types import ExactDecimal, UTCDateTime


def _sql_in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


_WORKING = _sql_in(tuple(s.value for s in WORKING_ORDER_STATUSES))
_EXITS = _sql_in(EXIT_PURPOSES)
_ALL_STATUSES = _sql_in(tuple(s.value for s in OrderStatus))


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[int]:
    return mapped_column(Integer, primary_key=True, autoincrement=True)


# ============================================================ market data
class Instrument(Base):
    """One row per (broker, internal key, trading date of the master file)."""

    __tablename__ = "instruments"
    id: Mapped[int] = _pk()
    key: Mapped[str] = mapped_column(String(80), index=True)  # InstrumentKey.canonical
    provider: Mapped[str] = mapped_column(String(20))  # fyers / flattrade
    master_date: Mapped[dt.date] = mapped_column(Date)
    broker_symbol: Mapped[str] = mapped_column(String(80))
    broker_token: Mapped[str | None] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(8))  # OPT/FUT/EQ/INDEX
    underlying: Mapped[str | None] = mapped_column(String(30))
    expiry: Mapped[dt.date | None] = mapped_column(Date)
    strike: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    option_type: Mapped[str | None] = mapped_column(String(2))
    isin: Mapped[str | None] = mapped_column(String(12))
    series: Mapped[str | None] = mapped_column(String(4))
    lot_size: Mapped[int | None] = mapped_column(Integer)
    tick_size: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    freeze_qty: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint("provider", "key", "master_date"),)


class Candle(Base):
    __tablename__ = "candles"
    id: Mapped[int] = _pk()
    instrument_key: Mapped[str] = mapped_column(String(80))
    timeframe: Mapped[str] = mapped_column(String(4))  # 1m / 15m / 1d
    ts: Mapped[dt.datetime] = mapped_column(UTCDateTime)  # candle open time
    open: Mapped[Decimal] = mapped_column(ExactDecimal)
    high: Mapped[Decimal] = mapped_column(ExactDecimal)
    low: Mapped[Decimal] = mapped_column(ExactDecimal)
    close: Mapped[Decimal] = mapped_column(ExactDecimal)
    volume: Mapped[int] = mapped_column(Integer, default=0)
    oi: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))  # single candle source (section 7.2)
    __table_args__ = (UniqueConstraint("instrument_key", "timeframe", "ts", "source"),)


class BackfillChunk(Base):
    """One finished backfill request, so an interrupted backfill resumes (section 16.4)."""

    __tablename__ = "backfill_chunks"
    id: Mapped[int] = _pk()
    series: Mapped[str] = mapped_column(String(80))  # instrument key canonical
    timeframe: Mapped[str] = mapped_column(String(4))
    start: Mapped[dt.date] = mapped_column(Date)
    end: Mapped[dt.date] = mapped_column(Date)
    rows: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))
    done_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("series", "timeframe", "start", "end", "source"),)


class Tick(Base):
    """Raw ticks; rotated by the recorder's retention policy (section 16.1)."""

    __tablename__ = "ticks"
    id: Mapped[int] = _pk()
    instrument_key: Mapped[str] = mapped_column(String(80))
    exchange_ts: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    received_ts: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    ltp: Mapped[Decimal] = mapped_column(ExactDecimal)
    bid: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    ask: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    bid_qty: Mapped[int | None] = mapped_column(Integer)
    ask_qty: Mapped[int | None] = mapped_column(Integer)
    cum_volume: Mapped[int | None] = mapped_column(Integer)
    oi: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))
    __table_args__ = (Index("ix_ticks_key_ts", "instrument_key", "exchange_ts"),)


class OptionChainSnapshot(Base):
    """One row per strike per snapshot. Stored from day one (section 7.4)."""

    __tablename__ = "option_chain_snapshots"
    id: Mapped[int] = _pk()
    snapshot_id: Mapped[str] = mapped_column(String(40), index=True)
    taken_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True)
    underlying: Mapped[str] = mapped_column(String(30))
    expiry: Mapped[dt.date] = mapped_column(Date)
    spot: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    india_vix: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    strike: Mapped[Decimal] = mapped_column(ExactDecimal)
    option_type: Mapped[str] = mapped_column(String(2))
    instrument_key: Mapped[str] = mapped_column(String(80))
    bid: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    ask: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    ltp: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    volume: Mapped[int | None] = mapped_column(Integer)
    oi: Mapped[int | None] = mapped_column(Integer)
    oi_change: Mapped[int | None] = mapped_column(Integer)
    iv: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    delta: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    source: Mapped[str] = mapped_column(String(20))


# ============================================================ strategies and config
class ConfigVersion(Base):
    __tablename__ = "config_versions"
    id: Mapped[int] = _pk()
    config_hash: Mapped[str] = mapped_column(String(16), unique=True)
    scope: Mapped[str] = mapped_column(String(60))  # "platform" or a strategy id
    body: Mapped[str] = mapped_column(Text)  # canonical JSON; never contains secrets
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    created_by: Mapped[str] = mapped_column(String(40))  # dashboard / telegram / file


class StrategyRow(Base):
    """The strategies table (section 6). Each change writes a new row in strategy_history."""

    __tablename__ = "strategies"
    id: Mapped[str] = mapped_column(String(60), primary_key=True)
    priority: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(120))
    segment: Mapped[str] = mapped_column(String(10))
    underlyings: Mapped[str] = mapped_column(String(200))  # comma-separated
    stage: Mapped[str] = mapped_column(String(10))
    code_status: Mapped[str] = mapped_column(String(12))
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    requested_mode: Mapped[str] = mapped_column(String(5), default="PAPER")
    gate_status: Mapped[str] = mapped_column(String(12), default="not_run")
    gate_passed_on: Mapped[dt.date | None] = mapped_column(Date)
    notes: Mapped[str] = mapped_column(Text, default="")
    config_hash: Mapped[str | None] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)


class StrategyHistory(Base):
    """Versioned copy of every strategies-table change (section 6.2)."""

    __tablename__ = "strategy_history"
    id: Mapped[int] = _pk()
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"))
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, object]] = mapped_column(JSON)
    changed_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    changed_by: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(Text, default="")


class StrategyGateReport(Base):
    __tablename__ = "strategy_gate_reports"
    id: Mapped[int] = _pk()
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"))
    book: Mapped[str] = mapped_column(String(30))
    evaluated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    period_start: Mapped[dt.date] = mapped_column(Date)
    period_end: Mapped[dt.date] = mapped_column(Date)
    thresholds: Mapped[dict[str, object]] = mapped_column(JSON)
    metrics: Mapped[dict[str, object]] = mapped_column(JSON)
    passed: Mapped[bool] = mapped_column(Boolean)
    overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    override_phrase_logged: Mapped[bool] = mapped_column(Boolean, default=False)


# ============================================================ signals and AI
class SignalRow(Base):
    __tablename__ = "signals"
    id: Mapped[int] = _pk()
    signal_uid: Mapped[str] = mapped_column(String(40), unique=True)
    strategy_id: Mapped[str] = mapped_column(String(60), index=True)
    config_hash: Mapped[str] = mapped_column(String(16))
    instance_id: Mapped[str] = mapped_column(String(4))
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True)
    underlying: Mapped[str] = mapped_column(String(30))
    direction: Mapped[str] = mapped_column(String(10))  # LONG_CE / LONG_PE / LONG
    inputs: Mapped[dict[str, object]] = mapped_column(JSON)  # full input snapshot
    checklist: Mapped[dict[str, object]] = mapped_column(JSON)  # each item's result
    outcome: Mapped[str] = mapped_column(String(20))  # TAKEN / SUPERSEDED / SKIPPED_* / ...


class AIVerdict(Base):
    __tablename__ = "ai_verdicts"
    id: Mapped[int] = _pk()
    signal_uid: Mapped[str] = mapped_column(ForeignKey("signals.signal_uid"))
    slot: Mapped[int] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(80))
    requested_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10))  # OK / INVALID / TIMEOUT / ERROR
    approve: Mapped[bool | None] = mapped_column(Boolean)
    confidence: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(ExactDecimal)


# ============================================================ orders, fills, positions
class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = _pk()
    position_uid: Mapped[str] = mapped_column(String(40), unique=True)
    instance_id: Mapped[str] = mapped_column(String(4))
    book: Mapped[str] = mapped_column(String(30), index=True)  # PAPER / REAL / shadow:X
    mode: Mapped[str] = mapped_column(String(5))  # mode the position was opened in
    broker: Mapped[str] = mapped_column(String(20))  # paper / semi / flattrade / fyers
    strategy_id: Mapped[str | None] = mapped_column(String(60))  # None if UNMANAGED
    signal_uid: Mapped[str | None] = mapped_column(String(40))
    instrument_key: Mapped[str] = mapped_column(String(80))
    product: Mapped[str] = mapped_column(String(4))
    state: Mapped[str] = mapped_column(String(16), index=True)
    qty_open: Mapped[int] = mapped_column(Integer, default=0)
    avg_entry_price: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    strategy_stop_underlying: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    target_underlying: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    backstop_sl_price: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    opened_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    self_reported: Mapped[bool] = mapped_column(Boolean, default=False)
    config_hash: Mapped[str | None] = mapped_column(String(16))
    # Invariant 1: never net short. A position's open quantity cannot go below zero.
    __table_args__ = (CheckConstraint("qty_open >= 0", name="ck_positions_never_short"),)


class Order(Base):
    """One row per order *intent*. `client_tag` is stored before sending (invariant 5)."""

    __tablename__ = "orders"
    id: Mapped[int] = _pk()
    client_tag: Mapped[str] = mapped_column(String(30), unique=True)
    instance_id: Mapped[str] = mapped_column(String(4))
    position_uid: Mapped[str] = mapped_column(ForeignKey("positions.position_uid"))
    book: Mapped[str] = mapped_column(String(30))
    mode: Mapped[str] = mapped_column(String(5))
    broker: Mapped[str] = mapped_column(String(20))
    broker_order_id: Mapped[str | None] = mapped_column(String(40), index=True)
    purpose: Mapped[str] = mapped_column(String(12))  # ENTRY / PROTECT / EXIT / FLATTEN
    instrument_key: Mapped[str] = mapped_column(String(80))
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(6))  # LMT / SL-LMT only
    product: Mapped[str] = mapped_column(String(4))
    qty: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[Decimal] = mapped_column(ExactDecimal)
    trigger_price: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    status: Mapped[str] = mapped_column(String(16))
    filled_qty: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    __table_args__ = (
        CheckConstraint("order_type IN ('LMT', 'SL-LMT')", name="ck_orders_limit_types_only"),
        CheckConstraint("side IN ('BUY', 'SELL')", name="ck_orders_side"),
        CheckConstraint(f"status IN {_ALL_STATUSES}", name="ck_orders_status"),
        CheckConstraint("qty > 0 AND filled_qty >= 0 AND filled_qty <= qty", name="ck_orders_qty"),
        # Invariant 2: at most one working exit-side order per position.
        Index(
            "uq_orders_one_working_exit",
            "position_uid",
            unique=True,
            sqlite_where=text(f"purpose IN {_EXITS} AND status IN {_WORKING}"),
            postgresql_where=text(f"purpose IN {_EXITS} AND status IN {_WORKING}"),
        ),
    )


class OrderEvent(Base):
    """Every state transition, with its reason (section 9.3)."""

    __tablename__ = "order_events"
    id: Mapped[int] = _pk()
    client_tag: Mapped[str] = mapped_column(ForeignKey("orders.client_tag"), index=True)
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    raw: Mapped[dict[str, object] | None] = mapped_column(JSON)  # scrubbed broker payload


class Fill(Base):
    __tablename__ = "fills"
    id: Mapped[int] = _pk()
    client_tag: Mapped[str] = mapped_column(ForeignKey("orders.client_tag"), index=True)
    # Always set: the broker's trade id, or "PAPER-<n>" / "SELF-<task_uid>" for paper and
    # self-reported fills, so the unique key below also de-duplicates those.
    broker_trade_id: Mapped[str] = mapped_column(String(40))
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    qty: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(ExactDecimal)
    self_reported: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("client_tag", "broker_trade_id"),)


class SemiTask(Base):
    """SEMI order task sent to the trader (invariant 15)."""

    __tablename__ = "semi_tasks"
    id: Mapped[int] = _pk()
    task_uid: Mapped[str] = mapped_column(String(20), unique=True)
    position_uid: Mapped[str] = mapped_column(ForeignKey("positions.position_uid"))
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    instruction: Mapped[dict[str, object]] = mapped_column(JSON)  # entry + protective SL
    status: Mapped[str] = mapped_column(String(16))  # SENT/CONFIRMED/SKIPPED/EXPIRED/SEEN
    confirmed_price: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    confirmed_via: Mapped[str | None] = mapped_column(String(20))  # trader / orderbook
    protective_stop_seen_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)


class HoldingsSnapshot(Base):
    __tablename__ = "holdings_snapshots"
    id: Mapped[int] = _pk()
    taken_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True)
    broker: Mapped[str] = mapped_column(String(20))
    instrument_key: Mapped[str] = mapped_column(String(80))
    qty: Mapped[int] = mapped_column(Integer)
    avg_price: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    raw: Mapped[dict[str, object] | None] = mapped_column(JSON)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = _pk()
    position_uid: Mapped[str] = mapped_column(ForeignKey("positions.position_uid"), unique=True)
    strategy_id: Mapped[str | None] = mapped_column(String(60), index=True)
    book: Mapped[str] = mapped_column(String(30), index=True)
    instrument_key: Mapped[str] = mapped_column(String(80))
    entry_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    exit_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    qty: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[Decimal] = mapped_column(ExactDecimal)
    exit_price: Mapped[Decimal] = mapped_column(ExactDecimal)
    gross_pnl: Mapped[Decimal] = mapped_column(ExactDecimal)
    charges: Mapped[dict[str, str]] = mapped_column(JSON)  # itemised, decimal strings
    charges_total: Mapped[Decimal] = mapped_column(ExactDecimal)
    net_pnl: Mapped[Decimal] = mapped_column(ExactDecimal)
    compliance_score: Mapped[int | None] = mapped_column(Integer)
    overrides: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    self_reported: Mapped[bool] = mapped_column(Boolean, default=False)
    charges_table_version: Mapped[str] = mapped_column(String(20))
    config_hash: Mapped[str | None] = mapped_column(String(16))


# ============================================================ risk, arming, sessions
class RiskState(Base):
    """Circuit-breaker state per book per period; survives restarts (invariant 8)."""

    __tablename__ = "risk_state"
    id: Mapped[int] = _pk()
    book: Mapped[str] = mapped_column(String(30))
    # day / week / month, or "streak": one row per losing streak (period_start = first loss).
    # A streak spans days; it ends on a win or a logged manual reset (DECISIONS.md D19).
    period: Mapped[str] = mapped_column(String(6))
    period_start: Mapped[dt.date] = mapped_column(Date)
    realized_pnl: Mapped[Decimal] = mapped_column(ExactDecimal, default=Decimal(0))
    worst_unrealized_pnl: Mapped[Decimal] = mapped_column(ExactDecimal, default=Decimal(0))
    trades: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    tripped: Mapped[bool] = mapped_column(Boolean, default=False)
    tripped_reason: Mapped[str | None] = mapped_column(Text)
    tripped_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    reset_override_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    reset_override_by: Mapped[str | None] = mapped_column(String(40))  # dashboard / telegram
    reset_override_reason: Mapped[str | None] = mapped_column(Text)  # typed confirmation
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("book", "period", "period_start"),)


class ArmingSession(Base):
    __tablename__ = "arming_sessions"
    id: Mapped[int] = _pk()
    instance_id: Mapped[str] = mapped_column(String(4))
    process_start_id: Mapped[str] = mapped_column(String(40))  # arming dies with the process
    trading_date: Mapped[dt.date] = mapped_column(Date)
    armed_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    armed_via: Mapped[str] = mapped_column(String(20))
    disarmed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    disarm_reason: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[dict[str, object]] = mapped_column(JSON)


class LoginSession(Base):
    """Metadata only. The token itself lives in the encrypted vault (invariant 18)."""

    __tablename__ = "login_sessions"
    id: Mapped[int] = _pk()
    provider: Mapped[str] = mapped_column(String(20))
    purpose: Mapped[str] = mapped_column(String(10))  # data / execution
    instance_id: Mapped[str] = mapped_column(String(4))
    trading_date: Mapped[dt.date] = mapped_column(Date)
    obtained_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    token_fingerprint: Mapped[str] = mapped_column(String(8))  # last chars of a hash only
    status: Mapped[str] = mapped_column(String(10))  # VALID / EXPIRED / REVOKED


class Instance(Base):
    __tablename__ = "instances"
    instance_id: Mapped[str] = mapped_column(String(4), primary_key=True)
    role: Mapped[str] = mapped_column(String(10))
    host_label: Mapped[str] = mapped_column(String(40))
    registered_static_ip: Mapped[str | None] = mapped_column(String(45))
    last_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    last_ip_check_ok: Mapped[bool | None] = mapped_column(Boolean)
    process_start_id: Mapped[str | None] = mapped_column(String(40))


# ============================================================ journal and research
class EventLog(Base):
    __tablename__ = "event_log"
    id: Mapped[int] = _pk()
    at: Mapped[dt.datetime] = mapped_column(UTCDateTime, index=True)
    category: Mapped[str] = mapped_column(String(10), index=True)  # SIGNAL/OI_FILTER/...
    severity: Mapped[str] = mapped_column(String(8))  # info / warn / critical
    instance_id: Mapped[str] = mapped_column(String(4))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, object] | None] = mapped_column(JSON)


class JournalEOD(Base):
    __tablename__ = "journal_eod"
    id: Mapped[int] = _pk()
    trading_date: Mapped[dt.date] = mapped_column(Date, unique=True)
    stats: Mapped[dict[str, object]] = mapped_column(JSON)
    ai_review: Mapped[str | None] = mapped_column(Text)
    ai_provider: Mapped[str | None] = mapped_column(String(20))
    trader_notes: Mapped[str | None] = mapped_column(Text)


class VariantResult(Base):
    __tablename__ = "variant_results"
    id: Mapped[int] = _pk()
    strategy_id: Mapped[str] = mapped_column(String(60))
    book: Mapped[str] = mapped_column(String(30))  # A / B / C / P / P+T
    period_start: Mapped[dt.date] = mapped_column(Date)
    period_end: Mapped[dt.date] = mapped_column(Date)
    trades: Mapped[int] = mapped_column(Integer)
    win_rate: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    expectancy_inr: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    profit_factor: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    max_drawdown_inr: Mapped[Decimal | None] = mapped_column(ExactDecimal)
    computed_at: Mapped[dt.datetime] = mapped_column(UTCDateTime)
    __table_args__ = (UniqueConstraint("strategy_id", "book", "period_start", "period_end"),)
