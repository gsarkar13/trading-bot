"""Wiring of the trading process (no network): slots, option watch list, setup."""

from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from app.adapters.base import TickData
from app.config.loader import load_config
from app.core.types import InstrumentKey
from app.services.engine_service import EngineService, build_slots, option_watch
from tests.engine_harness import activate, ist, smap


def test_build_slots_uses_implemented_plugins_and_their_warmup() -> None:
    cfg = load_config()
    slots = {s.id: s for s in build_slots(cfg)}
    assert {"ema200_hammer_rvol_oi", "pdh_pdl_breakout"} <= set(slots)
    assert slots["ema200_hammer_rvol_oi"].warm_1m >= 600 * 15  # 600 x 15m candles
    assert all(s.plugin.config_hash for s in slots.values())


def test_option_watch_is_atm_plus_minus_two_on_the_traded_expiry() -> None:
    cfg = load_config()
    keys = option_watch(smap(), cfg, "NIFTY", 24010.0, ist(10, 0))
    strikes = sorted({k.strike for k in keys})
    assert strikes[0] == Decimal(23850) and strikes[-1] == Decimal(24150)
    assert {k.expiry for k in keys} == {dt.date(2026, 12, 8)} and len(keys) == 14


class FakeRecorder:
    underlyings = ("NIFTY",)

    def __init__(self) -> None:
        from collections import defaultdict
        from types import SimpleNamespace

        self.stats = SimpleNamespace(socket_events=defaultdict(int))
        self.listeners: list[Any] = []
        self.watched: list[InstrumentKey] = []

    def watch(self, keys: list[InstrumentKey]) -> None:
        self.watched += keys


def test_setup_warms_up_reconciles_and_listens(migrated_engine: Engine, tmp_path: Path) -> None:
    cfg = load_config()
    activate(migrated_engine)
    rec = FakeRecorder()
    svc = EngineService(cfg, migrated_engine, smap(), None, rec, lambda: ist(9, 0), tmp_path)

    async def go() -> None:
        eng = await svc.setup()
        assert eng.slots and rec.listeners == [svc._on_tick]
        rec.listeners[0](
            TickData(
                key=InstrumentKey.index("NIFTY"),
                exchange_ts=ist(9, 15),
                received_ts=ist(9, 15),
                ltp=Decimal(24000),
            )
        )
        assert svc.queue.qsize() == 1

    asyncio.run(go())
    warn = [a.message for a in svc.alerts.recent if a.level == "warn"]
    assert any("warm-up candles stored" in m for m in warn)  # empty DB: fail closed


def test_dashboard_never_opens_extra_hosts_without_a_password(
    migrated_engine: Engine, tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = load_config().model_copy(deep=True)
    cfg = cfg.model_copy(
        update={"dashboard": cfg.dashboard.model_copy(update={"extra_bind_hosts": ("100.64.0.5",)})}
    )
    activate(migrated_engine)
    svc = EngineService(
        cfg, migrated_engine, smap(), None, FakeRecorder(), lambda: ist(9, 0), tmp_path
    )
    asyncio.run(svc.setup())
    assert [s.config.host for s in svc.dashboard_servers(None)] == ["127.0.0.1"]
    assert any("set a password" in a.message for a in svc.alerts.recent)
    hosts = [s.config.host for s in svc.dashboard_servers("scrypt$00$00")]
    assert hosts == ["127.0.0.1", "100.64.0.5"]
