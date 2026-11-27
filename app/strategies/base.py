"""Strategy plugin interface (brief section 6.2).

A plugin turns market data into *signals*. It never talks to a broker, never sizes a
position and never places an order: the engine, risk and execution layers do that.
Plugins must be deterministic given their inputs, so the same code runs in backtest,
replay, paper and live (section 14.1).
"""

from __future__ import annotations

import abc
import datetime as dt
from decimal import Decimal
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from app.config.loader import config_hash
from app.core.types import Segment


class DataNeeds(BaseModel):
    """What a plugin needs from the data layer. The engine checks these before enabling it."""

    model_config = ConfigDict(frozen=True)

    timeframes: tuple[Literal["1m", "15m", "1d"], ...]
    needs_futures_volume: bool = False
    needs_option_chain: bool = False
    needs_ticks: bool = False


class WarmupRequirement(BaseModel):
    """Bars of history per timeframe before the plugin may emit signals (invariant 6)."""

    model_config = ConfigDict(frozen=True)

    bars: dict[str, int]  # e.g. {"15m": 600, "1m": 600}
    prior_daily_candles: int = 0


class MarketView(BaseModel):
    """Read-only snapshot handed to a plugin on each event. Built by the engine.

    Phase 1 fills this with indicator state; Phase 0 fixes only the shape.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    underlying: str
    now: dt.datetime
    data: dict[str, object]


class Signal(BaseModel):
    model_config = ConfigDict(frozen=True)

    strategy_id: str
    underlying: str
    direction: Literal["LONG_CE", "LONG_PE", "LONG"]
    at: dt.datetime
    reference_price: Decimal  # underlying price at the signal
    stop_underlying: Decimal
    target_underlying: Decimal | None
    checklist: dict[str, object]  # each item's value and pass/fail, stored with the signal
    config_hash: str


class ExitDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Literal["HOLD", "EXIT_TARGET", "EXIT_STOP", "TRAIL"]
    new_stop_underlying: Decimal | None = None
    reason: str = ""


class StrategyPlugin(abc.ABC):
    """Subclass, set the class attributes, implement the handlers, then `register()` it."""

    id: ClassVar[str]
    segment: ClassVar[Segment]
    params_model: ClassVar[type[BaseModel]]

    def __init__(self, params: BaseModel) -> None:
        if not isinstance(params, self.params_model):
            raise TypeError(f"{self.id}: params must be {self.params_model.__name__}")
        self.params = params
        self.config_hash = config_hash(params)

    @classmethod
    @abc.abstractmethod
    def data_needs(cls, params: BaseModel) -> DataNeeds: ...

    @classmethod
    @abc.abstractmethod
    def warmup(cls, params: BaseModel) -> WarmupRequirement: ...

    @abc.abstractmethod
    def on_candle(self, timeframe: str, view: MarketView) -> list[Signal]:
        """Called on each completed candle, in strategy-priority order."""

    def on_tick(self, view: MarketView) -> list[Signal]:
        """Optional. Most plugins act on candle close only."""
        return []

    @abc.abstractmethod
    def exit_logic(self, position: dict[str, object], view: MarketView) -> ExitDecision:
        """Strategy-specific exit (target / trailing). Backstop stops live in the engine."""
