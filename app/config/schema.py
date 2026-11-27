"""Typed configuration schema.

Everything tunable lives here, with the defaults the trader chose (brief section 22).
Secrets are NOT part of this schema: they live in `.env` / the encrypted vault and are
loaded separately (see `app.config.secrets`), so they never enter config hashes, logs or
the database.

Times are IST wall-clock times; the trading calendar is in `config/calendar/`.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.types import InstanceRole, Mode, Segment
from app.strategies.seed import seed_row

Pct = Annotated[float, Field(ge=0, le=100)]
PosInt = Annotated[int, Field(gt=0)]
NonNegFloat = Annotated[float, Field(ge=0)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- host / instance
class InstanceConfig(_Strict):
    instance_id: Annotated[str, Field(pattern=r"^[A-Z0-9]{2,4}$")] = Field(
        default="LAP1",
        description="Short upper-case id (2-4 chars). Embedded in every order tag (invariant 5).",
    )
    instance_role: InstanceRole = InstanceRole.RESEARCH
    host_label: str = "laptop"
    registered_static_ip: str | None = Field(
        default=None, description="IPv4 registered with the brokers. None on the laptop."
    )
    ip_check_services: tuple[str, ...] = (
        "https://checkip.amazonaws.com",
        "https://api.ipify.org",
    )
    ip_recheck_minutes: PosInt = 10
    max_clock_drift_ms: PosInt = 500
    ntp_server: str = "pool.ntp.org"


# --------------------------------------------------------------------------- capital / risk
class SegmentAllocation(_Strict):
    enabled: bool = False
    options_inr: Decimal = Decimal("30000")
    stock_inr: Decimal = Decimal("20000")


class LossLimit(_Strict):
    pct_of_capital: Pct
    inr: Decimal | None = Field(
        default=None, description="If set, the tighter of this and the % limit applies."
    )


class RiskConfig(_Strict):
    capital_inr: Decimal = Decimal("50000")
    segment_allocation: SegmentAllocation = SegmentAllocation()
    daily_loss: LossLimit = LossLimit(pct_of_capital=3)
    weekly_loss: LossLimit = LossLimit(pct_of_capital=8)
    monthly_loss: LossLimit = LossLimit(pct_of_capital=12)
    max_trades_per_day: PosInt = 3
    max_consecutive_losses: PosInt = 3
    # Decided by the trader 2026-11-27 after the Phase 0 estimate (DECISIONS.md D12).
    risk_per_trade_inr: Decimal = Decimal("1000")
    max_lots: PosInt = 1
    max_concurrent_positions: PosInt = 1
    max_premium_pct_of_capital: Pct = 40
    cooldown_minutes_after_exit: Annotated[int, Field(ge=0)] = 10
    max_backstop_loss_multiple_of_risk: NonNegFloat = Field(
        default=1.5,
        description=(
            "Proposed in ARCHITECTURE.md: skip a trade if the broker-side backstop SL would "
            "lose more than this multiple of risk_per_trade."
        ),
    )


class TimeGuards(_Strict):
    no_entry_start: dt.time = dt.time(9, 15)
    no_entry_end: dt.time = dt.time(9, 30)
    last_entry: dt.time = dt.time(14, 45)
    square_off: dt.time = dt.time(15, 10)
    session_open: dt.time = dt.time(9, 15)
    session_close: dt.time = dt.time(15, 30)

    @model_validator(mode="after")
    def _order(self) -> TimeGuards:
        if not (
            self.session_open
            <= self.no_entry_start
            <= self.no_entry_end
            < self.last_entry
            < self.square_off
            < self.session_close
        ):
            raise ValueError("time guards are out of order")
        return self


class FreshnessGuards(_Strict):
    """Invariant 6: fail closed on stale or bad inputs."""

    max_tick_age_s: NonNegFloat = 5
    max_chain_age_s: NonNegFloat = 90
    candle_close_grace_s: NonNegFloat = 2


class RateLimits(_Strict):
    order_ops_per_second: Annotated[float, Field(gt=0, lt=10)] = 4
    data_requests_per_second: Annotated[float, Field(gt=0)] = 3
    data_requests_per_minute: PosInt = 45


class ExecutionMechanics(_Strict):
    protect_fill_within_s: NonNegFloat = 3
    entry_buffer_ticks: Annotated[int, Field(ge=0)] = 2
    exit_buffer_ticks: Annotated[int, Field(ge=0)] = 2
    reprice_after_s: NonNegFloat = 2
    max_reprices: Annotated[int, Field(ge=0)] = 3
    max_slippage_pct: Pct = 2
    gap_chase_after_s: NonNegFloat = 3
    gap_chase_step_ticks: PosInt = 4
    gap_chase_max_attempts: PosInt = 8
    flatten_max_attempts: PosInt = 10
    unmanaged_action: Literal["protect", "flatten"] = "protect"
    foreign_instance_check_s: PosInt = 60


class ModesConfig(_Strict):
    global_ceiling: Mode = Mode.PAPER
    semi_task_protective_stop_timeout_s: PosInt = 120
    semi_skip_mute_minutes: PosInt = 30


# --------------------------------------------------------------------------- options mechanics
class ContractSelection(_Strict):
    strike_mode: Literal["ATM", "ITM_1", "delta_target"] = "ATM"
    delta_target: Annotated[float, Field(gt=0, lt=1)] = 0.5
    nifty_expiry_day: Literal["skip", "next_weekly"] = "next_weekly"
    max_spread_pct: Pct = 1.5
    min_depth_lots: PosInt = 2
    # Backstop = broker-side SL-LMT on premium, wider than the strategy stop (section 9.2).
    # "delta": entry - (strategy stop distance x delta x multiple). "pct": entry x (1 - pct).
    # Default is delta: a fixed 35% backstop on a Rs 120 NIFTY ATM premium would risk
    # ~Rs 2,700 per lot, far beyond the risk budget (ARCHITECTURE.md section 7).
    backstop_mode: Literal["delta", "pct"] = "delta"
    backstop_delta_multiple: Annotated[float, Field(ge=1)] = 1.5
    backstop_sl_pct_of_premium: Pct = 35


# --------------------------------------------------------------------------- strategies
class TrendFilter(_Strict):
    timeframe_min: Literal[15] = 15
    ema_period: PosInt = 200
    strict_hammer: bool = False
    strict_n_bars: PosInt = 3


class Ema200HammerParams(_Strict):
    underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY")
    trend: TrendFilter = TrendFilter()
    ema_period_1m: PosInt = 200
    warmup_multiple: Annotated[float, Field(ge=1)] = 3
    tol_mode: Literal["points", "atr"] = "atr"
    tol_value: NonNegFloat = 0.25
    atr_period: PosInt = 14
    min_range_atr: NonNegFloat = 0.8
    lower_wick_to_body_min: NonNegFloat = 2.0
    opposite_wick_to_range_max: Annotated[float, Field(ge=0, le=1)] = 0.25
    rvol_source: Literal["futures", "option"] = "futures"
    rvol_mode: Literal["sma20", "time_of_day"] = "sma20"
    rvol_lookback: PosInt = 20
    rvol_threshold: NonNegFloat = 2.0
    futures_roll_days_before_expiry: Annotated[int, Field(ge=0)] = 3
    oi_filter_enabled: bool = True
    chain_interval_s: PosInt = 30
    chain_n_strikes: PosInt = 5
    oi_window_min: PosInt = 15
    oi_strikes_each_side: PosInt = 3
    oi_unknown_policy: Literal["block", "pass_flagged"] = "block"
    stop_buffer_points: NonNegFloat = 5
    target_mode: Literal["rr", "trail"] = "rr"
    target_rr: Annotated[float, Field(gt=0)] = 2.0


class PdhPdlParams(_Strict):
    underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY")
    buffer_mode: Literal["points", "atr"] = "atr"
    buffer_value: NonNegFloat = 0.25
    atr_period: PosInt = 14
    max_trades_per_direction_per_day: Literal[1] = 1
    trend_filter_enabled: bool = False
    trend: TrendFilter = TrendFilter()
    stop_mode: Literal["candle", "atr", "tighter_of_both"] = "tighter_of_both"
    stop_buffer_points: NonNegFloat = 5
    stop_atr_k: NonNegFloat = 1.5
    target_mode: Literal["rr", "trail"] = "rr"
    target_rr: Annotated[float, Field(gt=0)] = 2.0


# Params model for each implemented strategy id. Placeholders have none.
STRATEGY_PARAMS: dict[str, type[BaseModel]] = {
    "ema200_hammer_rvol_oi": Ema200HammerParams,
    "pdh_pdl_breakout": PdhPdlParams,
}


class StrategyEntry(_Strict):
    """One config block per strategy. `params` is validated by the plugin's own schema."""

    id: str
    active: bool = False
    requested_mode: Mode = Mode.PAPER
    allow_semi: bool = False
    semi_fallback: bool = False
    params: dict[str, object] = Field(default_factory=dict)


# --------------------------------------------------------------------------- gates
class GateThresholds(_Strict):
    """Phase 3 gate (brief section 21). Editable by the trader."""

    min_paper_trades: PosInt = 40
    min_weeks: PosInt = 4
    require_positive_expectancy: bool = True
    require_beats_baseline: bool = True
    max_drawdown_pct_of_capital: Pct = 12
    max_invariant_violations: Annotated[int, Field(ge=0)] = 0
    max_reconciliation_mismatches: Annotated[int, Field(ge=0)] = 0


# --------------------------------------------------------------------------- providers
Capability = Literal[
    "historical_candles", "live_ticks", "option_chain", "instrument_master", "execution"
]


class RoutingConfig(_Strict):
    """Which provider serves which capability. First entry is primary; the rest are failover."""

    historical_candles: tuple[str, ...] = ("fyers",)
    live_ticks: tuple[str, ...] = ("fyers",)
    option_chain: tuple[str, ...] = ("fyers",)
    instrument_master: tuple[str, ...] = ("fyers", "flattrade")
    execution: tuple[str, ...] = ("flattrade",)


class AISlot(_Strict):
    provider: Literal["gemini", "anthropic", "openai", "openrouter", "ollama", "none"]
    model: str
    timeout_ms: PosInt = 1500
    daily_call_cap: PosInt = 200


class AIConfig(_Strict):
    enabled: bool = False
    slot1: AISlot = AISlot(provider="gemini", model="UNSET-choose-in-phase-2")
    # Model IDs are chosen from each provider's docs in Phase 2 and recorded in DECISIONS.md.
    slot2: AISlot = AISlot(provider="anthropic", model="UNSET-choose-in-phase-2")


class TelegramConfig(_Strict):
    enabled: bool = False
    owner_chat_id: int | None = None
    login_reminder: dt.time = dt.time(8, 45)
    mute_paper_trade_alerts: bool = False
    engine_heartbeat_timeout_s: PosInt = 20


class RecorderConfig(_Strict):
    start: dt.time = dt.time(9, 0)
    stop: dt.time = dt.time(15, 35)
    raw_tick_retention_days: PosInt = 30
    underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY")


class DatabaseConfig(_Strict):
    url: str = "sqlite:///data/platform.db"
    sqlite_busy_timeout_ms: PosInt = 5000


class DashboardConfig(_Strict):
    bind_host: str = "127.0.0.1"
    port: PosInt = 8600
    extra_bind_hosts: tuple[str, ...] = Field(
        default=(), description="e.g. the Tailscale IP. Never 0.0.0.0 without HTTPS + login."
    )

    @model_validator(mode="after")
    def _no_public_bind(self) -> DashboardConfig:
        for h in (self.bind_host, *self.extra_bind_hosts):
            if h in ("0.0.0.0", "::"):  # noqa: S104 - this is the check that forbids it
                raise ValueError(
                    "binding to all interfaces is not allowed; bind 127.0.0.1 and the "
                    "Tailscale IP only (brief section 15)"
                )
        return self


class PlatformConfig(_Strict):
    schema_version: Literal[1] = 1
    instance: InstanceConfig = InstanceConfig()
    risk: RiskConfig = RiskConfig()
    time_guards: TimeGuards = TimeGuards()
    freshness: FreshnessGuards = FreshnessGuards()
    rate_limits: RateLimits = RateLimits()
    execution: ExecutionMechanics = ExecutionMechanics()
    modes: ModesConfig = ModesConfig()
    contracts: ContractSelection = ContractSelection()
    gate: GateThresholds = GateThresholds()
    routing: RoutingConfig = RoutingConfig()
    ai: AIConfig = AIConfig()
    telegram: TelegramConfig = TelegramConfig()
    recorder: RecorderConfig = RecorderConfig()
    database: DatabaseConfig = DatabaseConfig()
    dashboard: DashboardConfig = DashboardConfig()
    strategies: tuple[StrategyEntry, ...] = ()

    @model_validator(mode="after")
    def _semantic_checks(self) -> PlatformConfig:
        ids = [s.id for s in self.strategies]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate strategy id in config")
        for s in self.strategies:
            row = seed_row(s.id)
            if row is None:
                raise ValueError(f"strategy {s.id!r} has no strategies-table row")
            # A typo in a parameter name must fail loudly, never fall back to a default.
            model = STRATEGY_PARAMS.get(s.id)
            if model is None:
                if s.params:
                    raise ValueError(f"strategy {s.id}: placeholder takes no params")
            else:
                model.model_validate(s.params)
            # Invariant 14: options strategies never fall back to SEMI entries.
            if row.segment is Segment.OPTIONS and s.semi_fallback:
                raise ValueError(f"strategy {s.id}: semi_fallback is not allowed for options")
            # Invariant 10: every restart comes up in PAPER. The *requested* mode may be
            # stored; the effective mode is resolved at runtime. Here we only forbid SEMI
            # requests for strategies that disallow it.
            if s.requested_mode is Mode.SEMI and not s.allow_semi:
                raise ValueError(f"strategy {s.id}: SEMI requested but allow_semi=false")
        if self.risk.segment_allocation.enabled:
            alloc = self.risk.segment_allocation
            if alloc.options_inr + alloc.stock_inr > self.risk.capital_inr:
                raise ValueError("segment allocation exceeds capital")
        return self

    def strategy_params(self, strategy_id: str) -> BaseModel:
        """The validated params model (all defaults filled in) for one strategy."""
        entry = next(s for s in self.strategies if s.id == strategy_id)
        return STRATEGY_PARAMS[strategy_id].model_validate(entry.params)
