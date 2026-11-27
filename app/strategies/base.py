"""Strategy plugin interface (brief section 6.2).

A plugin turns market data into *signals*. It never talks to a broker, never sizes a
position and never places an order: the engine, risk and execution layers do that.
Plugins must be deterministic given their inputs, so the same code runs in backtest,
replay, paper and live (section 14.1).
"""

from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from app.core.types import Segment
from app.data.bars import Bar


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


@dataclass(frozen=True, slots=True)
class OIPulse:
    """Option-chain change over the last W minutes at ATM +/- k strikes (section 7.4)."""

    window_min: float
    put_oi_change: int
    call_oi_change: int
    pcr_start: float
    pcr_end: float
    age_s: float  # age of the newest snapshot when this was computed


@dataclass(frozen=True, slots=True)
class MarketView:
    """Read-only inputs for one completed 1-minute candle of one underlying.

    Built identically by the backtester, replay and (Phase 2) the live engine, so a plugin
    cannot tell which mode it runs in (section 14.1). A plugin builds its own indicators
    (including its 15-minute candles) from these bars.
    """

    underlying: str
    now: dt.datetime  # close time of `bar` (bar.ts + 1 minute)
    bar: Bar  # the completed 1-minute index candle
    futures_bar: Bar | None = None  # same-minute front-month futures candle (volume)
    option_volume: int | None = None  # same-minute volume of the ATM option, if known
    prior_day: Bar | None = None  # previous trading day's daily index candle
    oi_pulse: OIPulse | None = None  # None = UNKNOWN (stale or never recorded)
    volume_series_reset: bool = False  # a futures roll happened: restart RVOL lookback
    new_session: bool = False  # first candle of the trading day


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


@dataclass(frozen=True, slots=True)
class OpenPosition:
    """What a plugin may see about its own open trade (underlying terms only)."""

    direction: str
    entry_underlying: float
    stop: float
    target: float | None
    initial_risk: float
    opened_at: dt.datetime


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

    def __init__(self, params: BaseModel, config_hash: str) -> None:
        """`config_hash` is the engine's `trade_config_hash` (params + risk + contracts +
        time guards + execution + freshness); the plugin copies it into every Signal."""
        if not isinstance(params, self.params_model):
            raise TypeError(f"{self.id}: params must be {self.params_model.__name__}")
        self.params = params
        self.config_hash = config_hash

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

    def on_trade_opened(self, signal: Signal) -> None:  # noqa: B027 - optional hook
        """Called by the engine when one of this plugin's signals became a trade."""

    @abc.abstractmethod
    def exit_logic(self, position: OpenPosition, view: MarketView) -> ExitDecision:
        """Strategy-specific exit management (e.g. trailing). Stop and target hits are
        detected by the engine/backtester from `position.stop` / `position.target`."""
