"""Phase 2 gate chaos tests (brief section 21), run through the real engine on paper.

Covered elsewhere: foreign-instance order and restart reconciliation
(test_engine_restart.py), engine hang + flatten via the watchdog (test_watchdog.py),
lost order response (test_paper_router.py), random event storms (test_oms.py).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import signal
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from app.adapters.base import TickData
from app.brokers.paper import PaperBroker, PaperConfig
from app.config.schema import (
    InstanceConfig,
    ModesConfig,
    PlatformConfig,
    RiskConfig,
    StrategyEntry,
)
from app.core.types import InstanceRole, InstrumentKey, Mode, PositionState
from app.db.models import StrategyRow
from tests.engine_harness import OPT, Clock, Harness, activate, ist, smap

D = Decimal
SIG = ("LONG_CE", 24000, 23985, 24030)


def open_trade(db: Engine, tmp: Path, **kw: object) -> Harness:
    activate(db)
    h = Harness(db, tmp, **kw)  # type: ignore[arg-type]
    h.strategy.script[ist(10, 0).astimezone(dt.UTC)] = SIG
    asyncio.run(h.minute(10, 0, 24000))
    return h


def outcomes(h: Harness) -> list[str]:
    return [d.outcome for d in h.engine.state.decisions]


def test_feed_drops_mid_trade_no_entries_but_position_still_protected(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    h = open_trade(migrated_engine, tmp_path)
    h.strategy.script[ist(10, 2).astimezone(dt.UTC)] = SIG

    async def go() -> None:
        # the index feed dies after 10:01; a candle closes late from the timer only
        await h.index(24000, ist(10, 2, 0))
        h.at(ist(10, 3, 30))
        await h.engine.on_timer()
        assert "stale ticks" in outcomes(h)[-1]
        # the broker-side backstop still protects: the option collapses, the stop fills
        await h.option("100.00", "100.40")
        await h.option("99.20", "99.60")
        h.at(ist(10, 3, 40))
        await h.engine.on_timer()

    asyncio.run(go())
    assert not h.engine.positions  # closed by the resting SL-LMT, no index ticks needed


def test_duplicate_and_out_of_order_ticks(migrated_engine: Engine, tmp_path: Path) -> None:
    activate(migrated_engine)
    h = Harness(migrated_engine, tmp_path)
    seen: list[dt.datetime] = []
    orig = h.strategy.on_candle
    h.strategy.on_candle = lambda tf, v: (seen.append(v.bar.ts), orig(tf, v))[1]  # type: ignore[method-assign]

    async def tick(t: dt.datetime, px: float) -> None:
        h.at(t)
        await h.engine.on_tick(
            TickData(key=InstrumentKey.index("NIFTY"), exchange_ts=t, received_ts=t, ltp=D(str(px)))
        )

    async def go() -> None:
        await tick(ist(10, 0, 1), 24000)
        await tick(ist(10, 0, 1), 24000)  # exact duplicate
        await tick(ist(10, 0, 30), 24010)
        await tick(ist(10, 1, 3), 24005)  # closes 10:00
        await tick(ist(10, 0, 50), 23000)  # late tick for a closed minute: dropped
        await tick(ist(10, 2, 3), 24006)  # closes 10:01

    asyncio.run(go())
    assert seen == [ist(10, 0).astimezone(dt.UTC), ist(10, 1).astimezone(dt.UTC)]
    assert h.engine.builders["NIFTY"].stats.duplicates == 1
    assert h.engine.builders["NIFTY"].stats.late == 1


def test_order_timeout_is_looked_up_not_doubled(
    migrated_engine: Engine, tmp_path: Path, monkeypatch: object
) -> None:
    lost = {"n": 0}
    real = PaperBroker.place_order

    async def flaky(self: PaperBroker, req):  # type: ignore[no-untyped-def]
        res = await real(self, req)
        if lost["n"] == 0:
            lost["n"] += 1
            raise TimeoutError("response lost")
        return res

    import pytest

    mp = pytest.MonkeyPatch()
    mp.setattr(PaperBroker, "place_order", flaky)
    try:
        h = open_trade(migrated_engine, tmp_path)
        asyncio.run(h.engine.on_timer())
    finally:
        mp.undo()
    for p in h.engine.positions.values():
        entries = [o for o in p.machine.orders.values() if o.purpose == "ENTRY"]
        assert len(entries) == 1 and p.machine.long_qty == 65  # one order, found by its tag
        assert p.machine.state is PositionState.OPEN_PROTECTED


def test_partial_fill_keeps_the_part_and_sizes_the_stop(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    cfg = PlatformConfig(
        strategies=(StrategyEntry(id="pdh_pdl_breakout", active=True),),
        risk=RiskConfig(
            risk_per_trade_inr=D(2000), max_lots=2, max_backstop_loss_multiple_of_risk=2.0
        ),
    )
    activate(migrated_engine)
    h = Harness(migrated_engine, tmp_path, cfg=cfg, paper_cfg=lambda: PaperConfig(latency_s=0.5))
    h.strategy.script[ist(10, 0).astimezone(dt.UTC)] = SIG

    async def go() -> None:
        await h.minute(10, 0, 24000)  # 2 lots sent; acked after 0.5 s
        from app.brokers.paper import Depth

        h.at(ist(10, 1, 3.1))
        thin = Depth(D("120.00"), D("120.50"), 650, 65, D("120.2"), h.clock())
        await h.engine.on_option_depth(OPT, thin)  # acked; only one lot offered: fills 65
        away = Depth(D("124.00"), D("124.50"), 650, 650, D("124.2"), h.clock())
        for s in (3.0, 6.0, 9.0, 12.0):  # price runs beyond the 2% slippage cap
            h.at(ist(10, 1, 3.1 + s))
            await h.engine.on_option_depth(OPT, away)
            await h.engine.on_timer()

    asyncio.run(go())
    p = next(p for p in h.engine.positions.values() if p.book == "PAPER")
    m = p.machine
    assert m.long_qty == 65 and not m.working("ENTRY")  # rest of the entry cancelled
    assert m.protect is not None and m.protect.remaining == 65  # stop covers what we hold


def test_gap_skipped_stop_is_chased_until_flat(migrated_engine: Engine, tmp_path: Path) -> None:
    h = open_trade(migrated_engine, tmp_path)
    machines = [p.machine for p in h.engine.positions.values()]

    async def go() -> None:
        await h.option("80.00", "80.40")  # gaps straight through the stop's limit
        for i in range(1, 12):
            h.at(ist(10, 1, 3 + 3.5 * i))
            await h.option("80.00", "80.40")
            await h.engine.on_timer()

    asyncio.run(go())
    assert not h.engine.positions
    for m in machines:
        assert m.state is PositionState.CLOSED
        stops = [o for o in m.orders.values() if o.trigger is not None or o.chases]
        assert any(o.chases >= 1 for o in m.orders.values()), stops  # inv 4: it was chased


def test_token_expiry_mid_session_blocks_entries_keeps_managing(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    h = open_trade(migrated_engine, tmp_path)
    h.health.data_login = False  # token expired / auth error
    h.strategy.script[ist(10, 1).astimezone(dt.UTC)] = SIG

    async def go() -> None:
        await h.minute(10, 1, 24000)
        assert "token expired" in outcomes(h)[-1]
        await h.option("131.00", "131.40")
        await h.index(24031, ist(10, 2, 5))  # target still works
        await h.engine.on_timer()

    asyncio.run(go())
    assert not h.engine.positions


KILL9 = r"""
import asyncio, datetime as dt, sys, time
from pathlib import Path
from sqlalchemy import create_engine
sys.path.insert(0, sys.argv[3])
from tests.engine_harness import Harness, ist
db = create_engine(sys.argv[1])
h = Harness(db, Path(sys.argv[2]))
h.strategy.script[ist(10, 0).astimezone(dt.UTC)] = ("LONG_CE", 24000, 23985, 24030)
asyncio.run(h.minute(10, 0, 24000))
print("open", len(h.engine.positions), flush=True)
time.sleep(600)
"""


def test_kill_minus_9_with_open_position_then_restart(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    activate(migrated_engine)
    root = str(Path(__file__).parent.parent)
    proc = subprocess.Popen(  # noqa: S603 - fixed test script, our own interpreter
        [sys.executable, "-c", KILL9, str(migrated_engine.url), str(tmp_path), root],
        stdout=subprocess.PIPE,
        text=True,
        cwd=root,
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "open 2"
        if hasattr(signal, "SIGKILL"):
            os.kill(proc.pid, signal.SIGKILL)  # no cleanup, no flush, nothing
        else:  # pragma: no cover - Windows
            proc.kill()
        proc.wait(timeout=5)
    finally:
        proc.kill()
    h = Harness(migrated_engine, tmp_path, clock=Clock(ist(10, 4)), process_id="after-kill")
    asyncio.run(h.engine.start())
    ps = list(h.engine.positions.values())
    assert len(ps) == 2 and all(p.machine.state is PositionState.OPEN_PROTECTED for p in ps)
    assert not h.engine.arming.is_armed(h.clock())


def test_static_ip_flips_mid_session_live_stops_entries_and_alerts(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    cfg = PlatformConfig(
        strategies=(StrategyEntry(id="pdh_pdl_breakout", active=True, requested_mode=Mode.LIVE),),
        instance=InstanceConfig(
            instance_role=InstanceRole.EXECUTION, registered_static_ip="13.200.1.1"
        ),
        modes=ModesConfig(global_ceiling=Mode.LIVE),
    )
    activate(migrated_engine)
    with Session(migrated_engine) as s:
        s.execute(
            update(StrategyRow)
            .where(StrategyRow.id == "pdh_pdl_breakout")
            .values(requested_mode="LIVE", gate_status="passed")
        )
        s.commit()
    clock = Clock(ist(9, 59))
    live = PaperBroker({OPT: smap().info(OPT)}, clock, PaperConfig(latency_s=0.0))
    h = Harness(migrated_engine, tmp_path, clock=clock, cfg=cfg, live=live)
    h.health.static_ip_ok = h.health.exec_login = True
    eng = h.engine
    code = eng.arming.request(clock(), "test")
    assert eng.arming.confirm(clock(), code, "test", {})
    h.strategy.script[ist(10, 0).astimezone(dt.UTC)] = SIG
    h.strategy.script[ist(10, 2).astimezone(dt.UTC)] = SIG

    async def go() -> None:
        from app.brokers.paper import Depth

        live.on_market(OPT, Depth(D("120.00"), D("120.50"), 650, 650, D("120.2"), clock()))
        await h.minute(10, 0, 24000)
        assert outcomes(h)[-1] == "OPENED REAL LIVE"
        h.health.static_ip_ok = False  # the IP check flips mid-session
        await h.option("131.00", "131.40")
        live.on_market(OPT, Depth(D("131.00"), D("131.40"), 650, 650, D("131.2"), clock()))
        await h.index(24031, ist(10, 1, 10))  # the open LIVE trade is still managed
        await h.engine.on_timer()
        await h.minute(10, 2, 24000)

    asyncio.run(go())
    assert not [p for p in eng.positions.values() if p.book == "REAL"]
    assert "LIVE lost" in outcomes(h)[-1]  # inv 14: no silent downgrade to PAPER
    assert any(
        a.level == "critical" and "LIVE lost mid-session" in a.message for a in h.alerts.recent
    )


def test_all_order_operations_share_one_rate_limit(migrated_engine: Engine, tmp_path: Path) -> None:
    h = open_trade(migrated_engine, tmp_path)
    eng = h.engine
    live = eng._router("REAL", Mode.SEMI) if eng.semi else None
    assert live is None or live.limiter is eng.order_limiter
    assert eng.order_limiter.per_second == 4  # default, well under 10/s (inv 12)
    assert all(
        r.limiter is eng.paper_limiter for n, r in eng.routers.items() if n.startswith("paper:")
    )


def test_candle_gap_blocks_entries_for_fifteen_minutes(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    activate(migrated_engine)
    h = Harness(migrated_engine, tmp_path)
    for m in (5, 21):
        h.strategy.script[ist(10, m).astimezone(dt.UTC)] = SIG

    async def go() -> None:
        await h.minute(10, 0, 24000)
        h.at(ist(10, 4, 59))
        for m in range(5, 22):  # 10:01-10:04 missing, then continuous
            await h.minute(10, m, 24000)

    asyncio.run(go())
    outs = outcomes(h)
    assert "gap in candles" in outs[0]  # 10:05, right after the gap
    assert not outs[1].startswith("BLOCKED: gap")  # 10:21, window over
