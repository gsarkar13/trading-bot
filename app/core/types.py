"""Shared domain types used by every layer.

These are plain value types with no broker or database knowledge.
Instruments are identified by an internal key, never by a broker's symbol
string (brief section 14.3).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class Mode(StrEnum):
    """Execution mode. Ordered from safest to most dangerous."""

    PAPER = "PAPER"
    SEMI = "SEMI"
    LIVE = "LIVE"

    @property
    def rank(self) -> int:
        return _MODE_RANK[self]

    @property
    def is_real_money(self) -> bool:
        return self is not Mode.PAPER

    @staticmethod
    def lowest(*modes: Mode) -> Mode:
        return min(modes, key=lambda m: m.rank)


_MODE_RANK = {Mode.PAPER: 0, Mode.SEMI: 1, Mode.LIVE: 2}


class Segment(StrEnum):
    OPTIONS = "options"
    STOCK = "stock"


class InstanceRole(StrEnum):
    EXECUTION = "execution"
    RESEARCH = "research"


class Product(StrEnum):
    MIS = "MIS"  # intraday, squared off by the time guard
    CNC = "CNC"  # delivery, held across days
    NRML = "NRML"  # may appear in broker positions (e.g. manual trades); never sent by us


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    """Only limit-type orders exist here. Market orders are out of scope (section 24)."""

    LMT = "LMT"
    SL_LMT = "SL-LMT"


class OrderStatus(StrEnum):
    """Our record of one order intent (table `orders.status`)."""

    NEW = "NEW"  # stored with its tag, not yet sent (invariant 5)
    SENT = "SENT"  # sent, no answer yet
    UNCERTAIN = "UNCERTAIN"  # timeout / error: look up the tag before anything else
    PENDING = "PENDING"  # resting at the broker
    TRIGGER_PENDING = "TRIGGER_PENDING"  # SL-LMT waiting for its trigger
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


WORKING_ORDER_STATUSES: tuple[OrderStatus, ...] = (
    OrderStatus.NEW,
    OrderStatus.SENT,
    OrderStatus.UNCERTAIN,  # might be live at the broker, so it counts as working
    OrderStatus.PENDING,
    OrderStatus.TRIGGER_PENDING,
    OrderStatus.PARTIAL,
)

# Purposes of orders that sell an open long. Invariant 2: at most one may be working.
EXIT_PURPOSES: tuple[str, ...] = ("PROTECT", "EXIT", "FLATTEN")


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"


class StrategyStage(StrEnum):
    IDEA = "idea"
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"
    DROPPED = "dropped"
    EXCLUDED = "excluded"


class CodeStatus(StrEnum):
    IMPLEMENTED = "implemented"
    PLACEHOLDER = "placeholder"


class GateStatus(StrEnum):
    NOT_RUN = "not_run"
    FAILING = "failing"
    PASSED = "passed"
    OVERRIDDEN = "overridden"


class PositionState(StrEnum):
    """Order/position lifecycle (section 9.3). Transitions are enforced in Phase 2."""

    SIGNALLED = "SIGNALLED"
    ENTRY_PENDING = "ENTRY_PENDING"
    PARTIAL = "PARTIAL"
    OPEN_PROTECTED = "OPEN_PROTECTED"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    UNMANAGED = "UNMANAGED"
    SUPERSEDED = "SUPERSEDED"
    SKIPPED_SIZE = "SKIPPED_SIZE"
    SEMI_TASK_SENT = "SEMI_TASK_SENT"
    SELF_REPORTED = "SELF_REPORTED"
    ERROR = "ERROR"


class InstrumentKey(BaseModel):
    """Broker-neutral instrument identity.

    One of three shapes:
      * option:  (underlying, expiry, strike, CE/PE)
      * future:  (underlying, expiry, FUT)
      * equity:  (NSE, ISIN, series)
      * index:   (underlying) with kind="INDEX" — spot indices have no tradable contract
      * FUTC:    (underlying) — Fyers' continuous front-month future series (history only,
                 used for volume). Data only: never tradable
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["OPT", "FUT", "EQ", "INDEX", "FUTC"]
    underlying: str | None = None
    expiry: dt.date | None = None
    strike: Decimal | None = None
    option_type: OptionType | None = None
    isin: str | None = None
    series: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> InstrumentKey:
        k = self.kind
        has = {
            "underlying": self.underlying is not None,
            "expiry": self.expiry is not None,
            "strike": self.strike is not None,
            "option_type": self.option_type is not None,
            "isin": self.isin is not None,
            "series": self.series is not None,
        }
        required = {
            "OPT": {"underlying", "expiry", "strike", "option_type"},
            "FUT": {"underlying", "expiry"},
            "EQ": {"isin", "series"},
            "INDEX": {"underlying"},
            "FUTC": {"underlying"},
        }[k]
        present = {name for name, ok in has.items() if ok}
        if present != required:
            raise ValueError(f"{k} key needs exactly {sorted(required)}, got {sorted(present)}")
        if self.strike is not None and self.strike <= 0:
            raise ValueError("strike must be positive")
        if self.underlying is not None and self.underlying != self.underlying.upper():
            raise ValueError("underlying must be upper case, e.g. NIFTY")
        return self

    @property
    def canonical(self) -> str:
        """Stable string form for database keys and logs. Not a broker symbol."""
        if self.kind == "OPT":
            assert self.expiry and self.strike is not None and self.option_type
            return (
                f"OPT|{self.underlying}|{self.expiry.isoformat()}|"
                f"{self.strike.normalize():f}|{self.option_type.value}"
            )
        if self.kind == "FUT":
            assert self.expiry
            return f"FUT|{self.underlying}|{self.expiry.isoformat()}"
        if self.kind == "EQ":
            return f"EQ|NSE|{self.isin}|{self.series}"
        if self.kind == "FUTC":
            return f"FUTC|{self.underlying}"
        return f"INDEX|{self.underlying}"

    @property
    def tradable(self) -> bool:
        return self.kind in ("OPT", "FUT", "EQ")

    @classmethod
    def option(
        cls, underlying: str, expiry: dt.date, strike: Decimal | int | str, opt: OptionType
    ) -> InstrumentKey:
        return cls(
            kind="OPT",
            underlying=underlying,
            expiry=expiry,
            strike=Decimal(str(strike)),
            option_type=opt,
        )

    @classmethod
    def future(cls, underlying: str, expiry: dt.date) -> InstrumentKey:
        return cls(kind="FUT", underlying=underlying, expiry=expiry)

    @classmethod
    def equity(cls, isin: str, series: str = "EQ") -> InstrumentKey:
        return cls(kind="EQ", isin=isin, series=series)

    @classmethod
    def index(cls, underlying: str) -> InstrumentKey:
        return cls(kind="INDEX", underlying=underlying)

    @classmethod
    def continuous_future(cls, underlying: str) -> InstrumentKey:
        return cls(kind="FUTC", underlying=underlying)
