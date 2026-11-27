"""Plugin registry (brief section 6.2). Seed rows live in `app.strategies.seed`.

A strategy's `code_status` is *derived*: it is `implemented` only if a plugin class is
registered for its id. That makes "only implemented strategies can be switched on"
(section 6.2) impossible to get wrong by editing a table row.
"""

from __future__ import annotations

from app.core.types import CodeStatus, StrategyStage
from app.strategies.base import StrategyPlugin
from app.strategies.seed import SEED_IDS, SEED_ROWS, SeedRow, seed_row

__all__ = [
    "SEED_IDS",
    "SEED_ROWS",
    "SeedRow",
    "can_activate",
    "code_status",
    "get_plugin",
    "register",
    "seed_row",
]

_REGISTRY: dict[str, type[StrategyPlugin]] = {}


def register(cls: type[StrategyPlugin]) -> type[StrategyPlugin]:
    if cls.id in _REGISTRY and _REGISTRY[cls.id] is not cls:
        raise ValueError(f"duplicate plugin id {cls.id!r}")
    row = seed_row(cls.id)
    if row is None:
        raise ValueError(f"plugin {cls.id!r} has no strategies-table row; add one to SEED_ROWS")
    if cls.segment is not row.segment:
        raise ValueError(f"plugin {cls.id!r} segment {cls.segment} != table row {row.segment}")
    _REGISTRY[cls.id] = cls
    return cls


def get_plugin(strategy_id: str) -> type[StrategyPlugin] | None:
    return _REGISTRY.get(strategy_id)


def code_status(strategy_id: str) -> CodeStatus:
    return CodeStatus.IMPLEMENTED if strategy_id in _REGISTRY else CodeStatus.PLACEHOLDER


def can_activate(strategy_id: str) -> tuple[bool, str]:
    """Section 6.2: only implemented strategies can be switched on.

    Exposure checks (invariant 17) are added by the engine in Phase 2.
    """
    if strategy_id not in SEED_IDS:
        return False, "unknown strategy"
    if code_status(strategy_id) is not CodeStatus.IMPLEMENTED:
        return False, "placeholder: no code yet"
    row = seed_row(strategy_id)
    assert row is not None
    if row.stage in (StrategyStage.DROPPED, StrategyStage.EXCLUDED):
        return False, f"stage is {row.stage.value}"
    return True, "ok"
