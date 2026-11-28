"""Telegram bot and watchdog (section 13), including the chaos case: the engine hangs
with positions open and /flatten still works through the watchdog."""

from __future__ import annotations

import asyncio
import datetime as dt
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.adapters.base import InstrumentInfo
from app.brokers.paper import Depth, PaperBroker
from app.config.schema import PlatformConfig, TelegramConfig
from app.core.types import InstrumentKey
from app.db.models import Instance
from app.engine.alerts import AlertBus
from app.services.recorder import save_master
from app.services.watchdog import Watchdog, default_kill
from app.telegram.link import EngineDown, EngineLink, EngineRefused
from tests.engine_harness import OPT, Clock, Harness, activate, ist, smap

OWNER = 4242


class FakeTg:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send(self, chat_id: int, text: str, buttons: Any = None) -> None:
        self.sent.append((chat_id, text))

    async def answer(self, callback_id: str, text: str = "") -> None:
        pass


class FakeLink:
    def __init__(self, down: bool = False) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.down = down

    async def get(self, path: str) -> Any:
        return await self.call("GET", path)

    async def post(self, path: str, body: Any = None) -> Any:
        return await self.call("POST", path, body)

    async def call(self, method: str, path: str, body: Any = None) -> Any:
        if self.down:
            raise EngineDown("ConnectError")
        self.calls.append((method, path, body))
        if path == "/api/arm/request":
            return {
                "code": "1234",
                "summary": {
                    "host": "pc",
                    "role": "research",
                    "static_ip_ok": False,
                    "ceiling": "PAPER",
                },
            }
        if path == "/api/flatten":
            return {"positions": 2}
        if path == "/api/ceiling":
            raise EngineRefused("config ceiling is PAPER; raise it in config first")
        return {}


def cfg() -> PlatformConfig:
    return PlatformConfig(
        telegram=TelegramConfig(enabled=True, owner_chat_id=OWNER, engine_heartbeat_timeout_s=20)
    )


def msg(text: str, chat: int = OWNER) -> dict[str, Any]:
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def wd(db: Engine, tmp: Path, link: Any, clock: Clock, **kw: Any) -> tuple[Watchdog, FakeTg]:
    tg = FakeTg()

    async def quote(k: InstrumentKey) -> Depth | None:
        return Depth(Decimal("125.00"), Decimal("125.40"), 650, 650, Decimal("125.2"), clock())

    w = Watchdog(cfg(), db, tg, link, clock, tmp / "engine.lock", tmp, quote, **kw)
    return w, tg


def test_only_the_owner_is_obeyed(migrated_engine: Engine, tmp_path: Path) -> None:
    link = FakeLink()
    w, tg = wd(migrated_engine, tmp_path, link, Clock(ist(10, 0)))
    asyncio.run(w.bot.handle(msg("/pause", chat=999)))
    assert link.calls == [] and tg.sent == []
    asyncio.run(w.bot.handle(msg("/pause")))
    assert link.calls[0][1] == "/api/engine/pause"
    asyncio.run(w.bot.handle(msg("/mode live")))
    assert "Refused: config ceiling" in tg.sent[-1][1]


def test_unconfigured_bot_tells_you_your_chat_id(migrated_engine: Engine, tmp_path: Path) -> None:
    w, tg = wd(migrated_engine, tmp_path, FakeLink(), Clock(ist(10, 0)))
    w.bot.owner = None
    asyncio.run(w.bot.handle(msg("/start", chat=777)))
    assert tg.sent == [(777, tg.sent[0][1])] and "777" in tg.sent[0][1]


def test_arm_and_flatten_are_two_step(migrated_engine: Engine, tmp_path: Path) -> None:
    clock = Clock(ist(10, 0))
    link = FakeLink()
    w, tg = wd(migrated_engine, tmp_path, link, clock)

    async def go() -> None:
        await w.bot.handle(msg("/arm"))
        assert "1234" in tg.sent[-1][1]
        await w.bot.handle(msg("1234"))
        assert ("POST", "/api/arm/confirm", {"code": "1234"}) in link.calls
        await w.bot.handle(msg("/flatten"))
        clock.t += dt.timedelta(seconds=90)
        await w.bot.handle(msg("FLATTEN"))
        assert "Too late" in tg.sent[-1][1]
        await w.bot.handle(msg("/flatten"))
        await w.bot.handle(msg("FLATTEN"))
        assert "2 position(s)" in tg.sent[-1][1]

    asyncio.run(go())


def test_alerts_relayed_and_heartbeat_watched(migrated_engine: Engine, tmp_path: Path) -> None:
    clock = Clock(ist(10, 0))
    w, tg = wd(migrated_engine, tmp_path, FakeLink(), clock)
    bus = AlertBus(migrated_engine, "LAP1", clock)
    bus.emit("critical", "RISK", "PAPER: breaker: day loss")
    bus.emit("info", "TRADE", "SHADOW:pdh_pdl_breakout PAPER: opened")  # not relayed
    bus.emit("info", "TRADE", "PAPER PAPER: pdh_pdl_breakout LONG_CE opened")
    bus.emit("info", "SEMI", "T0001 new: BUY 65 ...")
    asyncio.run(w.relay_alerts())
    texts = [t for _, t in tg.sent]
    assert len(texts) == 3 and "breaker" in texts[0] and not any("SHADOW" in t for t in texts)
    with Session(migrated_engine) as s:
        s.add(
            Instance(
                instance_id="LAP1", role="research", host_label="pc", last_seen_at=ist(9, 59, 55)
            )
        )
        s.commit()
    asyncio.run(w.check_heartbeat())
    assert len(tg.sent) == 3  # 5 s old: fine
    clock.t = ist(10, 1)
    asyncio.run(w.check_heartbeat())
    asyncio.run(w.check_heartbeat())
    assert sum("HEARTBEAT LOST" in t for _, t in tg.sent) == 1  # once, not every pass


HOLDER = """
import sys, time
from pathlib import Path
from app.engine.instance import InstanceLock
InstanceLock(Path(sys.argv[1])).acquire()
print("locked", flush=True)
time.sleep(600)  # a hung engine: holds the lock, answers nothing
"""


def test_chaos_engine_hangs_and_watchdog_flattens(migrated_engine: Engine, tmp_path: Path) -> None:
    activate(migrated_engine)
    h = Harness(migrated_engine, tmp_path)
    h.strategy.script[ist(10, 0).astimezone(dt.UTC)] = ("LONG_CE", 24000, 23985, 24030)
    asyncio.run(h.minute(10, 0, 24000))
    assert len(h.engine.positions) == 2
    info: InstrumentInfo = smap().info(OPT)
    save_master(migrated_engine, [info], dt.date(2026, 12, 2))
    lock = tmp_path / "engine.lock"
    proc = subprocess.Popen(  # noqa: S603 - fixed test script, our own interpreter
        [sys.executable, "-c", HOLDER, str(lock)],
        stdout=subprocess.PIPE,
        cwd=Path(__file__).parent.parent,
        text=True,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "locked"
        link = EngineLink(1, tmp_path / "no.token", timeout_s=0.5)  # nothing answers
        w, _tg = wd(migrated_engine, tmp_path, link, h.clock, kill=default_kill)
        w.flatten_offline = _fast_offline
        t0 = time.monotonic()
        out = asyncio.run(w.emergency_flatten())
        assert time.monotonic() - t0 < 15
        assert "stopped it and flattened directly" in out, out
        assert "STILL OPEN" not in out
        proc.wait(timeout=5)  # the hung engine was stopped
    finally:
        proc.kill()
    for f in ("PAPER.json", "SHADOW_pdh_pdl_breakout.json"):
        b = PaperBroker({}, h.clock, state_path=tmp_path / f)
        assert asyncio.run(b.positions()) == []


async def _fast_offline(*a: Any, **kw: Any) -> Any:
    from app.tools.flatten import flatten_paper_books

    return await flatten_paper_books(*a, pause_s=0.0, **kw)
