"""AI advisory (section 11, invariant 9): async, fail-over, caps, scrubbing, what-if."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.ai.advisor import Advisor, what_if
from app.ai.providers import ProviderError, RateLimited, make_provider
from app.config.schema import AIConfig, AISlot
from app.config.secrets import Secrets
from app.db.models import AIVerdict
from tests.engine_harness import Harness, activate, ist

OK = '{"approve": false, "confidence": 70, "reason": "late in the move"}'


class Fake:
    def __init__(
        self, name: str, reply: str = OK, delay: float = 0.0, raises: Exception | None = None
    ) -> None:
        self.name, self.reply, self.delay, self.raises = name, reply, delay, raises
        self.seen: list[str] = []

    async def complete(
        self, system: str, user: str, schema: dict[str, Any], timeout_s: float, max_out: int
    ) -> tuple[str, int, int]:
        self.seen.append(user)
        await asyncio.sleep(self.delay)
        if self.raises:
            raise self.raises
        return self.reply, 400, 30


def cfg(cap: int = 200) -> AIConfig:
    return AIConfig(
        enabled=True,
        slot1=AISlot(provider="gemini", model="g", timeout_ms=200, daily_call_cap=cap),
        slot2=AISlot(
            provider="anthropic",
            model="h",
            timeout_ms=200,
            price_in_usd_per_m=0.10,
            price_out_usd_per_m=0.50,
        ),
    )


def verdicts(db: Engine) -> list[AIVerdict]:
    with Session(db) as s:
        return list(s.scalars(select(AIVerdict).order_by(AIVerdict.id)))


def run_signal(
    db: Engine, tmp: Path, advisor_providers: dict[int, Any], config: AIConfig | None = None
) -> Harness:
    activate(db)
    h = Harness(db, tmp)
    adv = Advisor(config or cfg(), db, advisor_providers, h.clock)
    h.engine.on_signal = adv.submit
    h.strategy.script[ist(10, 0).astimezone(dt.UTC)] = ("LONG_CE", 24000, 23985, 24030)

    async def go() -> None:
        await h.minute(10, 0, 24000)
        # invariant 9: the trade is already open while the AI is still thinking
        assert len(h.engine.positions) == 2
        assert verdicts(db) == []
        await asyncio.gather(*adv._tasks)

    asyncio.run(go())
    return h


def test_slow_ai_never_delays_the_order_and_times_out_to_slot2(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    slow, ok = Fake("gemini", delay=5.0), Fake("anthropic")
    run_signal(migrated_engine, tmp_path, {1: slow, 2: ok})
    v = verdicts(migrated_engine)
    assert [(x.slot, x.status) for x in v] == [(1, "TIMEOUT"), (2, "OK")]
    assert v[1].approve is False and v[1].confidence == 70
    assert v[1].cost_usd is not None and float(v[1].cost_usd) == (400 * 0.10 + 30 * 0.50) / 1e6


def test_rate_limit_and_errors_fail_over_but_invalid_does_not(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    run_signal(
        migrated_engine,
        tmp_path,
        {
            1: Fake("gemini", raises=RateLimited("429")),
            2: Fake("anthropic", reply='{"approve": "maybe"}'),
        },
    )
    assert [(x.slot, x.status) for x in verdicts(migrated_engine)] == [
        (1, "RATE_LIMIT"),
        (2, "INVALID"),
    ]


def test_daily_cap_and_error(migrated_engine: Engine, tmp_path: Path) -> None:
    run_signal(
        migrated_engine,
        tmp_path,
        {1: Fake("gemini"), 2: Fake("anthropic", raises=ProviderError("x"))},
        cfg(cap=1),
    )
    with Session(migrated_engine) as s:
        sig = s.scalars(select(AIVerdict.signal_uid)).first()
    adv = Advisor(
        cfg(cap=1),
        migrated_engine,
        {1: Fake("gemini"), 2: Fake("anthropic", raises=ProviderError("x"))},
        lambda: ist(10, 5),
    )
    assert sig is not None
    asyncio.run(adv.review_signal(sig, {"x": 1}))
    assert [x.status for x in verdicts(migrated_engine)] == ["OK", "CAPPED", "ERROR"]


def test_snapshot_is_scrubbed_before_it_leaves(migrated_engine: Engine, tmp_path: Path) -> None:
    f = Fake("gemini")
    activate(migrated_engine)
    adv = Advisor(cfg(), migrated_engine, {1: f, 2: None}, lambda: ist(10, 0))
    h = run_signal(migrated_engine, tmp_path, {1: Fake("gemini"), 2: None})
    with Session(migrated_engine) as s:
        sig = s.scalars(select(AIVerdict.signal_uid)).first()
    assert sig is not None
    leak = "access_token=" + "eyJhbGciOiJIUzI1NiJ9" + ".abcdefghijklmnopqrstuvwxyz0123456789"
    asyncio.run(adv.review_signal(sig, {"note": leak}))
    assert "eyJhbGci" not in f.seen[0]
    assert h.engine.state.signals == 1


def test_what_if_groups_shadow_trades_by_verdict(migrated_engine: Engine, tmp_path: Path) -> None:
    h = run_signal(migrated_engine, tmp_path, {1: Fake("gemini"), 2: None})

    async def close() -> None:
        await h.option("131.00", "131.40")
        await h.index(24031, ist(10, 1, 10))
        await h.engine.on_timer()

    asyncio.run(close())
    w = what_if(migrated_engine)
    assert w["disapproved"]["trades"] == 1 and w["approved"]["trades"] == 0
    assert float(str(w["disapproved"]["net_pnl"])) > 0  # the AI was wrong this time


def test_no_keys_means_no_providers_and_nothing_breaks() -> None:
    s = Secrets(_env_file=None)  # type: ignore[call-arg]
    c = AIConfig()
    assert make_provider(c.slot1, s) is None and make_provider(c.slot2, s) is None
    adv = Advisor(AIConfig(enabled=True), None, {1: None, 2: None}, lambda: ist(10, 0))  # type: ignore[arg-type]
    assert not adv.enabled
    adv.submit("x", {})  # no-op
