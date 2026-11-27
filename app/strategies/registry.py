"""Plugin registry and the seed strategies table (brief section 6.3).

A strategy's `code_status` is *derived*: it is `implemented` only if a plugin class is
registered for its id. That makes "only implemented strategies can be switched on"
(section 6.2) impossible to get wrong by editing a table row.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.types import CodeStatus, Segment, StrategyStage
from app.strategies.base import StrategyPlugin

_REGISTRY: dict[str, type[StrategyPlugin]] = {}


def register(cls: type[StrategyPlugin]) -> type[StrategyPlugin]:
    if cls.id in _REGISTRY and _REGISTRY[cls.id] is not cls:
        raise ValueError(f"duplicate plugin id {cls.id!r}")
    if cls.id not in SEED_IDS:
        raise ValueError(f"plugin {cls.id!r} has no strategies-table row; add one to SEED_ROWS")
    _REGISTRY[cls.id] = cls
    return cls


def get_plugin(strategy_id: str) -> type[StrategyPlugin] | None:
    return _REGISTRY.get(strategy_id)


def code_status(strategy_id: str) -> CodeStatus:
    return CodeStatus.IMPLEMENTED if strategy_id in _REGISTRY else CodeStatus.PLACEHOLDER


@dataclass(frozen=True)
class SeedRow:
    priority: int
    id: str
    name: str
    segment: Segment
    underlyings: tuple[str, ...]
    stage: StrategyStage
    target_code_status: CodeStatus  # what this build will deliver (Phase 1 for 1 and 2)
    notes: str


_IDX = ("NIFTY", "BANKNIFTY")
_N200 = ("NIFTY200-universe",)

SEED_ROWS: tuple[SeedRow, ...] = (
    SeedRow(
        1,
        "ema200_hammer_rvol_oi",
        "EMA200 hammer + RVOL + OI pulse",
        Segment.OPTIONS,
        _IDX,
        StrategyStage.BACKTEST,
        CodeStatus.IMPLEMENTED,
        "Primary hypothesis under test",
    ),
    SeedRow(
        2,
        "pdh_pdl_breakout",
        "PDH/PDL breakout",
        Segment.OPTIONS,
        _IDX,
        StrategyStage.BACKTEST,
        CodeStatus.IMPLEMENTED,
        "Baseline benchmark every strategy must beat",
    ),
    SeedRow(
        3,
        "nr7_breakout",
        "NR7 breakout",
        Segment.OPTIONS,
        _IDX,
        StrategyStage.IDEA,
        CodeStatus.PLACEHOLDER,
        "Strongest edge idea from earlier research (volatility expansion after a "
        "narrow day); next to build",
    ),
    SeedRow(
        4,
        "orb",
        "Opening range breakout (ORB)",
        Segment.OPTIONS,
        _IDX,
        StrategyStage.IDEA,
        CodeStatus.PLACEHOLDER,
        "Most trades, so quickest to prove or reject statistically",
    ),
    SeedRow(
        5,
        "monthly_momentum",
        "Monthly momentum rotation",
        Segment.STOCK,
        _N200,
        StrategyStage.IDEA,
        CodeStatus.PLACEHOLDER,
        "Hold top 2-3 Nifty 200 stocks by 6/12-month return, rebalance monthly",
    ),
    SeedRow(
        6,
        "vwap_pullback",
        "VWAP pullback momentum",
        Segment.OPTIONS,
        _IDX,
        StrategyStage.IDEA,
        CodeStatus.PLACEHOLDER,
        "A Pine Script version exists",
    ),
    SeedRow(
        7,
        "orb_adx",
        "ORB + ADX filter",
        Segment.OPTIONS,
        _IDX,
        StrategyStage.IDEA,
        CodeStatus.PLACEHOLDER,
        "A Pine Script version exists",
    ),
    SeedRow(
        8,
        "swing_pullback",
        "Swing pullback in uptrend",
        Segment.STOCK,
        _N200,
        StrategyStage.IDEA,
        CodeStatus.PLACEHOLDER,
        "Buy short dips above the 200-day average, hold 3-10 days",
    ),
    SeedRow(
        9,
        "intraday_stock_breakout",
        "Intraday stock breakout",
        Segment.STOCK,
        _N200,
        StrategyStage.IDEA,
        CodeStatus.PLACEHOLDER,
        "Cheapest per trade, hardest edge",
    ),
    SeedRow(
        10,
        "initial_balance",
        "Initial Balance",
        Segment.OPTIONS,
        _IDX,
        StrategyStage.DROPPED,
        CodeStatus.PLACEHOLDER,
        "Dropped: redundant with ORB, worse entries for a buyer",
    ),
    SeedRow(
        11,
        "liquidity_sweep_mss",
        "Liquidity sweep + MSS",
        Segment.OPTIONS,
        _IDX,
        StrategyStage.EXCLUDED,
        CodeStatus.PLACEHOLDER,
        "Excluded: signal rules are subjective, can't be backtested fairly",
    ),
)

SEED_IDS = frozenset(r.id for r in SEED_ROWS)


def can_activate(strategy_id: str) -> tuple[bool, str]:
    """Section 6.2: only implemented strategies can be switched on.

    Exposure checks (invariant 17) are added by the engine in Phase 2.
    """
    if strategy_id not in SEED_IDS:
        return False, "unknown strategy"
    if code_status(strategy_id) is not CodeStatus.IMPLEMENTED:
        return False, "placeholder: no code yet"
    row = next(r for r in SEED_ROWS if r.id == strategy_id)
    if row.stage in (StrategyStage.DROPPED, StrategyStage.EXCLUDED):
        return False, f"stage is {row.stage.value}"
    return True, "ok"
