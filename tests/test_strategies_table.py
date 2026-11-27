import datetime as dt
from decimal import Decimal
from typing import ClassVar

import pytest
from pydantic import BaseModel

from app.config.schema import PdhPdlParams
from app.core.types import CodeStatus, Segment, StrategyStage
from app.strategies import registry
from app.strategies.base import (
    DataNeeds,
    ExitDecision,
    MarketView,
    Signal,
    StrategyPlugin,
    WarmupRequirement,
)


def test_seed_rows_match_brief_section_6_3() -> None:
    rows = registry.SEED_ROWS
    assert [r.priority for r in rows] == list(range(1, 12))
    assert len({r.id for r in rows}) == 11
    assert rows[0].id == "ema200_hammer_rvol_oi" and rows[1].id == "pdh_pdl_breakout"
    implemented = {r.id for r in rows if r.target_code_status is CodeStatus.IMPLEMENTED}
    assert implemented == {"ema200_hammer_rvol_oi", "pdh_pdl_breakout"}
    stock = {r.id for r in rows if r.segment is Segment.STOCK}
    assert stock == {"monthly_momentum", "swing_pullback", "intraday_stock_breakout"}
    assert next(r for r in rows if r.id == "initial_balance").stage is StrategyStage.DROPPED
    assert next(r for r in rows if r.id == "liquidity_sweep_mss").stage is StrategyStage.EXCLUDED


def test_placeholders_cannot_be_switched_on() -> None:
    for r in registry.SEED_ROWS:
        if registry.get_plugin(r.id) is None:
            ok, why = registry.can_activate(r.id)
            assert not ok and "placeholder" in why
    assert registry.can_activate("nope") == (False, "unknown strategy")


class _Dummy(StrategyPlugin):
    id: ClassVar[str] = "pdh_pdl_breakout"
    segment: ClassVar[Segment] = Segment.OPTIONS
    params_model: ClassVar[type[BaseModel]] = PdhPdlParams

    @classmethod
    def data_needs(cls, params: BaseModel) -> DataNeeds:
        return DataNeeds(timeframes=("1m", "1d"))

    @classmethod
    def warmup(cls, params: BaseModel) -> WarmupRequirement:
        return WarmupRequirement(bars={"1m": 14}, prior_daily_candles=1)

    def on_candle(self, timeframe: str, view: MarketView) -> list[Signal]:
        return [
            Signal(
                strategy_id=self.id,
                underlying=view.underlying,
                direction="LONG_CE",
                at=view.now,
                reference_price=Decimal(1),
                stop_underlying=Decimal(0),
                target_underlying=None,
                checklist={},
                config_hash=self.config_hash,
            )
        ]

    def exit_logic(self, position: dict[str, object], view: MarketView) -> ExitDecision:
        return ExitDecision(action="HOLD")


def test_registering_a_plugin_makes_it_implemented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", {})
    assert registry.code_status("pdh_pdl_breakout") is CodeStatus.PLACEHOLDER
    registry.register(_Dummy)
    assert registry.code_status("pdh_pdl_breakout") is CodeStatus.IMPLEMENTED
    assert registry.can_activate("pdh_pdl_breakout") == (True, "ok")
    plugin = _Dummy(PdhPdlParams())
    sig = plugin.on_candle(
        "1m", MarketView(underlying="NIFTY", now=dt.datetime(2026, 11, 27, tzinfo=dt.UTC), data={})
    )[0]
    assert sig.config_hash == plugin.config_hash  # every signal carries its config hash


def test_plugin_without_table_row_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", {})

    class NoRow(_Dummy):
        id: ClassVar[str] = "not_in_table"

    with pytest.raises(ValueError, match="no strategies-table row"):
        registry.register(NoRow)


def test_plugin_rejects_wrong_params_type() -> None:
    from app.config.schema import Ema200HammerParams

    with pytest.raises(TypeError):
        _Dummy(Ema200HammerParams())
