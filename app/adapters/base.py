"""Adapter interfaces (brief section 14.2).

Rules every adapter follows:
  * Instruments cross this boundary only as `InstrumentKey`; each adapter maps keys to its
    own symbols/tokens through its own instrument master (section 14.3).
  * Every request goes through the adapter's token bucket (invariant 12).
  * Execution adapters accept only LMT and SL-LMT orders (section 3.1).
  * Every order carries a `client_tag` that was stored before sending (invariant 5).
  * Adapters never log or return secrets (invariant 18).
"""

from __future__ import annotations

import abc
import datetime as dt
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.tags import parse_tag
from app.core.types import InstrumentKey, OptionType, OrderType, Product, Side


class Capability(StrEnum):
    HISTORICAL_CANDLES = "historical_candles"
    LIVE_TICKS = "live_ticks"
    OPTION_CHAIN = "option_chain"
    INSTRUMENT_MASTER = "instrument_master"
    EXECUTION = "execution"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


# ----------------------------------------------------------------- data models
class InstrumentInfo(_Frozen):
    key: InstrumentKey
    broker_symbol: str
    broker_token: str | None
    lot_size: int
    tick_size: Decimal
    freeze_qty: int | None = None


class CandleBar(_Frozen):
    ts: dt.datetime  # candle open time, UTC-aware
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    oi: int | None = None


class TickData(_Frozen):
    key: InstrumentKey
    exchange_ts: dt.datetime
    received_ts: dt.datetime
    ltp: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    cum_volume: int | None = None
    oi: int | None = None


class ChainRow(_Frozen):
    strike: Decimal
    option_type: OptionType
    key: InstrumentKey
    bid: Decimal | None
    ask: Decimal | None
    ltp: Decimal | None
    volume: int | None
    oi: int | None
    oi_change: int | None = None
    iv: Decimal | None = None
    delta: Decimal | None = None


class ChainSnapshot(_Frozen):
    underlying: str
    expiry: dt.date
    taken_at: dt.datetime
    spot: Decimal | None
    india_vix: Decimal | None
    rows: tuple[ChainRow, ...]
    available_expiries: tuple[dt.date, ...] = ()


class OrderRequest(_Frozen):
    """An order intent. Validation here is the last check before any adapter sees it."""

    client_tag: str
    key: InstrumentKey
    side: Side
    qty: int = Field(gt=0)
    order_type: OrderType
    limit_price: Decimal = Field(gt=0)
    trigger_price: Decimal | None = None
    product: Product

    @model_validator(mode="after")
    def _check(self) -> OrderRequest:
        if parse_tag(self.client_tag) is None:  # invariants 5 and 16
            raise ValueError("client_tag must be <instance_id><YYMMDD><seq> (app.core.tags)")
        if not self.key.tradable:
            raise ValueError(f"{self.key.kind} is not a tradable contract")
        if self.product not in (Product.MIS, Product.CNC):
            raise ValueError("only MIS and CNC orders are sent in this build")
        if self.order_type is OrderType.LMT and self.trigger_price is not None:
            raise ValueError("LMT orders take no trigger price")
        if self.order_type is OrderType.SL_LMT:
            if self.trigger_price is None or self.trigger_price <= 0:
                raise ValueError("SL-LMT needs a positive trigger price")
            # A sell stop triggers at or above its limit; a buy stop at or below.
            if self.side is Side.SELL and self.trigger_price < self.limit_price:
                raise ValueError("SL-LMT sell: trigger must be >= limit")
            if self.side is Side.BUY and self.trigger_price > self.limit_price:
                raise ValueError("SL-LMT buy: trigger must be <= limit")
        return self


class BrokerOrderStatus(StrEnum):
    PENDING = "PENDING"  # accepted, resting
    TRIGGER_PENDING = "TRIGGER_PENDING"  # SL-LMT waiting for trigger
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class BrokerOrder(_Frozen):
    broker_order_id: str
    client_tag: str | None
    key: InstrumentKey | None  # None if the adapter could not map the symbol
    side: Side
    qty: int
    filled_qty: int
    avg_fill_price: Decimal | None
    limit_price: Decimal | None
    trigger_price: Decimal | None
    order_type: str
    status: BrokerOrderStatus
    reject_reason: str | None = None
    updated_at: dt.datetime | None = None


class BrokerPosition(_Frozen):
    key: InstrumentKey | None
    broker_symbol: str
    product: Product
    net_qty: int  # positive = long. Negative must never happen (invariant 1).
    avg_price: Decimal | None
    realized_pnl: Decimal | None = None


class BrokerHolding(_Frozen):
    key: InstrumentKey | None
    broker_symbol: str
    qty: int
    avg_price: Decimal | None


class Funds(_Frozen):
    available_cash: Decimal
    used_margin: Decimal | None = None


class PlaceResult(_Frozen):
    """Outcome of a send. `UNCERTAIN` means look up `client_tag` before any retry."""

    outcome: Literal["ACCEPTED", "REJECTED", "UNCERTAIN"]
    broker_order_id: str | None = None
    message: str | None = None


# ----------------------------------------------------------------- errors
class AdapterError(Exception):
    """Base class. Messages must never contain secrets."""


class AuthExpiredError(AdapterError):
    pass


class RateLimitedError(AdapterError):
    pass


class InstrumentMappingError(AdapterError):
    pass


class OrderNotAllowedError(AdapterError):
    """Raised before anything is sent, e.g. a market order or an unsupported product."""


# ----------------------------------------------------------------- interfaces
class BaseProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    def capabilities(self) -> frozenset[Capability]: ...

    @abc.abstractmethod
    async def health(self) -> bool:
        """Cheap check used by the router for failover. Never raises."""


class BaseDataProvider(BaseProvider):
    async def historical_candles(
        self,
        key: InstrumentKey,
        timeframe: Literal["1m", "15m", "1d"],
        start: dt.date,
        end: dt.date,
    ) -> Sequence[CandleBar]:
        raise NotImplementedError(f"{self.name} has no historical_candles")

    def live_ticks(self, keys: Sequence[InstrumentKey]) -> AsyncIterator[TickData]:
        raise NotImplementedError(f"{self.name} has no live_ticks")

    async def option_chain(
        self, underlying: str, expiry: dt.date | None, n_strikes: int
    ) -> ChainSnapshot:
        raise NotImplementedError(f"{self.name} has no option_chain")

    async def instrument_master(self, trading_date: dt.date) -> Sequence[InstrumentInfo]:
        raise NotImplementedError(f"{self.name} has no instrument_master")


class BaseExecutionBroker(BaseProvider):
    """Place / modify / cancel plus read-only account state."""

    @abc.abstractmethod
    async def place_order(self, req: OrderRequest) -> PlaceResult: ...

    @abc.abstractmethod
    async def modify_order(
        self,
        broker_order_id: str,
        *,
        limit_price: Decimal | None = None,
        trigger_price: Decimal | None = None,
        qty: int | None = None,
        order_type: OrderType | None = None,
    ) -> PlaceResult: ...

    @abc.abstractmethod
    async def cancel_order(self, broker_order_id: str) -> PlaceResult: ...

    @abc.abstractmethod
    async def order_book(self) -> Sequence[BrokerOrder]: ...

    @abc.abstractmethod
    async def positions(self) -> Sequence[BrokerPosition]: ...

    @abc.abstractmethod
    async def holdings(self) -> Sequence[BrokerHolding]: ...

    @abc.abstractmethod
    async def funds(self) -> Funds: ...

    @abc.abstractmethod
    async def instrument_master(self, trading_date: dt.date) -> Sequence[InstrumentInfo]: ...

    async def find_by_tag(self, client_tag: str) -> BrokerOrder | None:
        """Invariant 5: after a timeout, look the tag up before any retry."""
        for o in await self.order_book():
            if o.client_tag == client_tag:
                return o
        return None


class AIRequest(_Frozen):
    kind: Literal["signal_review", "eod_postmortem"]
    payload: dict[str, object]  # must already be scrubbed of secrets/account data


class AIResponse(_Frozen):
    status: Literal["OK", "INVALID", "TIMEOUT", "ERROR"]
    approve: bool | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    reason: str | None = None
    text: str | None = None  # EOD journal
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int | None = None


class BaseAIProvider(abc.ABC):
    """Advisory only (invariant 9). Nothing in the order path may await this."""

    name: str
    shares_data_for_training: bool = False  # True for Gemini free tier (section 2.6)

    @abc.abstractmethod
    async def review(self, req: AIRequest, timeout_ms: int) -> AIResponse: ...
