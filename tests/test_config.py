import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.config.loader import DEFAULT_FILES, config_hash, load_config
from app.config.schema import (
    DashboardConfig,
    Ema200HammerParams,
    PdhPdlParams,
    PlatformConfig,
    TimeGuards,
)
from app.config.secrets import Secrets
from app.core.types import InstanceRole, Mode


def test_committed_default_loads_and_matches_section_22() -> None:
    cfg = load_config((DEFAULT_FILES[0],))
    r = cfg.risk
    assert r.capital_inr == Decimal("50000")
    assert not r.segment_allocation.enabled
    assert (
        r.daily_loss.pct_of_capital,
        r.weekly_loss.pct_of_capital,
        r.monthly_loss.pct_of_capital,
    ) == (3, 8, 12)
    assert r.max_trades_per_day == 3 and r.max_consecutive_losses == 3
    assert r.max_lots == 1 and r.max_concurrent_positions == 1
    assert cfg.rate_limits.order_ops_per_second == 4
    tg = cfg.time_guards
    assert (tg.no_entry_start, tg.no_entry_end, tg.last_entry, tg.square_off) == (
        dt.time(9, 15),
        dt.time(9, 30),
        dt.time(14, 45),
        dt.time(15, 10),
    )
    assert cfg.telegram.login_reminder == dt.time(8, 45)
    assert cfg.routing.execution[0] == "flattrade"
    assert cfg.routing.historical_candles[0] == "fyers"
    assert cfg.modes.global_ceiling is Mode.PAPER
    assert all(not s.allow_semi for s in cfg.strategies)  # SEMI off for options
    assert cfg.ai.slot1.provider == "gemini" and cfg.ai.slot2.provider == "anthropic"
    assert cfg.instance.instance_role is InstanceRole.RESEARCH  # safe default
    assert cfg.gate.min_paper_trades == 40 and cfg.gate.min_weeks == 4


def test_host_override_deep_merges(tmp_path: Path) -> None:
    host = tmp_path / "host.yaml"
    host.write_text(
        "instance: {instance_id: AWS1, instance_role: execution}\nrisk: {max_lots: 2}\n"
    )
    cfg = load_config((DEFAULT_FILES[0], host))
    assert cfg.instance.instance_id == "AWS1"
    assert cfg.instance.instance_role is InstanceRole.EXECUTION
    assert cfg.risk.max_lots == 2
    assert cfg.risk.daily_loss.pct_of_capital == 3  # untouched sibling kept


def test_unknown_keys_rejected(tmp_path: Path) -> None:
    f = tmp_path / "x.yaml"
    f.write_text("risk: {max_lot: 2}\n")  # typo must not be silently ignored
    with pytest.raises(ValidationError):
        load_config((f,))


def test_public_bind_rejected() -> None:
    with pytest.raises(ValidationError):
        DashboardConfig(bind_host="0.0.0.0")  # noqa: S104
    with pytest.raises(ValidationError):
        DashboardConfig(extra_bind_hosts=("::",))


def test_time_guards_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        TimeGuards(last_entry=dt.time(15, 20))


def test_semi_request_needs_allow_semi() -> None:
    with pytest.raises(ValidationError):
        PlatformConfig.model_validate(
            {"strategies": [{"id": "x", "requested_mode": "SEMI", "allow_semi": False}]}
        )


def test_duplicate_strategy_ids_rejected() -> None:
    with pytest.raises(ValidationError):
        PlatformConfig.model_validate({"strategies": [{"id": "a"}, {"id": "a"}]})


def test_instance_id_format_enforced() -> None:
    with pytest.raises(ValidationError):
        PlatformConfig.model_validate({"instance": {"instance_id": "lap-1"}})


def test_config_hash_deterministic_and_sensitive() -> None:
    a, b = Ema200HammerParams(), Ema200HammerParams()
    assert config_hash(a) == config_hash(b)
    assert config_hash(a) != config_hash(Ema200HammerParams(rvol_threshold=2.5))
    assert config_hash(PdhPdlParams()) != config_hash(PdhPdlParams(trend_filter_enabled=True))
    assert len(config_hash(a)) == 16


def test_secrets_never_shown() -> None:
    s = Secrets(fyers_app_secret=SecretStr("super-secret-value-123"))
    assert "super-secret" not in repr(s) and "super-secret" not in str(s)
    assert "fyers_app_secret" in repr(s)
