"""The advisor: verdicts on signals and the end-of-day review (brief section 11).

Invariant 9: AI never delays, blocks, opens, sizes or modifies an order. `submit()` only
schedules a background task and returns at once; the engine never awaits it and never
reads its result. Verdicts are stored and shown; the "what-if" table compares the trades
the AI disapproved of with the rest, the evidence for ever promoting it to a veto.

Fail-over: slot 1, then slot 2 on a timeout, HTTP 429 or error. A response that doesn't
validate is stored as INVALID (no fail-over: the provider answered). Each slot has a daily
call cap and a token cost tracker. Snapshots are scrubbed of anything token-like before
they leave the host (invariant 18).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.ai.providers import Provider, ProviderError, RateLimited
from app.config.schema import AIConfig, AISlot
from app.core.scrub import redact
from app.core.timeutil import IST
from app.db.models import AIVerdict, JournalEOD, Position, SignalRow, Trade

log = logging.getLogger(__name__)

SYSTEM = (
    "You review intraday NIFTY option-buying signals for a small retail account. "
    "You get one JSON snapshot of the market and the strategy's checklist. Judge only "
    "from it: is this a reasonable entry? Reply with approve (true/false), confidence "
    "0-100 and a reason of at most 25 words. Your answer is logged for research; it does "
    "not place or block any trade."
)
REVIEW_SYSTEM = (
    "You write a short end-of-day trading journal entry for a retail options trader. "
    "From the day's ledger and rule-compliance data: what went to plan, what didn't, "
    "and one concrete thing to watch tomorrow. At most 150 words. No predictions."
)
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "approve": {"type": "boolean"},
        "confidence": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["approve", "confidence", "reason"],
    "additionalProperties": False,
}
REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"entry": {"type": "string"}},
    "required": ["entry"],
    "additionalProperties": False,
}


class Verdict(BaseModel):
    approve: bool
    confidence: int = Field(ge=0, le=100)
    reason: str = Field(max_length=500)


@dataclass
class Outcome:
    slot: int
    status: str  # OK / INVALID / TIMEOUT / ERROR / RATE_LIMIT / CAPPED
    verdict: Verdict | None = None
    latency_ms: int | None = None


class Advisor:
    def __init__(
        self,
        cfg: AIConfig,
        engine: Engine,
        providers: dict[int, Provider | None],
        now: Callable[[], dt.datetime],
    ) -> None:
        self.cfg, self.engine, self.providers, self.now = cfg, engine, providers, now
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled and any(self.providers.values())

    def _slot(self, n: int) -> AISlot:
        return self.cfg.slot1 if n == 1 else self.cfg.slot2

    def _calls_today(self, n: int) -> int:
        a = dt.datetime.combine(self.now().astimezone(IST).date(), dt.time(0), IST)
        with Session(self.engine) as s:
            return int(
                s.scalar(
                    select(func.count(AIVerdict.id)).where(
                        AIVerdict.slot == n,
                        AIVerdict.requested_at >= a,
                        AIVerdict.status != "CAPPED",
                    )
                )
                or 0
            )

    # ------------------------------------------------------------ signals
    def submit(self, signal_uid: str, snapshot: dict[str, Any]) -> None:
        """Fire and forget (invariant 9). Safe to call from the engine's event loop."""
        if not self.enabled:
            return
        try:
            task = asyncio.get_running_loop().create_task(self.review_signal(signal_uid, snapshot))
        except RuntimeError:
            return  # no running loop (e.g. a synchronous tool): skip, never block
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def review_signal(self, signal_uid: str, snapshot: dict[str, Any]) -> None:
        user = redact(json.dumps(snapshot, sort_keys=True, default=str))
        for n in (1, 2):
            out = await self._ask(n, signal_uid, user)
            if out is None or out.status in ("OK", "INVALID"):
                return
        return

    async def _ask(self, n: int, signal_uid: str, user: str) -> Outcome | None:
        p = self.providers.get(n)
        if p is None:
            return None
        slot = self._slot(n)
        if self._calls_today(n) >= slot.daily_call_cap:
            out = Outcome(n, "CAPPED")
            self._store(signal_uid, n, slot, p, out, None, None)
            return out
        t0 = time.monotonic()
        tin = tout = None
        try:
            text, tin, tout = await asyncio.wait_for(
                p.complete(SYSTEM, user, VERDICT_SCHEMA, slot.timeout_ms / 1000, 200),
                timeout=slot.timeout_ms / 1000 + 0.25,
            )
            try:
                out = Outcome(n, "OK", Verdict.model_validate_json(text))
            except ValidationError:
                out = Outcome(n, "INVALID")
        except TimeoutError:
            out = Outcome(n, "TIMEOUT")
        except RateLimited:
            out = Outcome(n, "RATE_LIMIT")
        except ProviderError as e:
            log.warning("AI slot %d: %s", n, e)
            out = Outcome(n, "ERROR")
        except Exception:  # an AI failure must never surface anywhere else
            log.exception("AI slot %d failed", n)
            out = Outcome(n, "ERROR")
        out.latency_ms = int((time.monotonic() - t0) * 1000)
        self._store(signal_uid, n, slot, p, out, tin, tout)
        return out

    def _store(
        self,
        uid: str,
        n: int,
        slot: AISlot,
        p: Provider,
        o: Outcome,
        tin: int | None,
        tout: int | None,
    ) -> None:
        cost = None
        if tin is not None or tout is not None:
            cost = (
                Decimal(str(slot.price_in_usd_per_m)) * (tin or 0)
                + Decimal(str(slot.price_out_usd_per_m)) * (tout or 0)
            ) / 1_000_000
        v = o.verdict
        with Session(self.engine) as s:
            s.add(
                AIVerdict(
                    signal_uid=uid,
                    slot=n,
                    provider=p.name,
                    model=slot.model,
                    requested_at=self.now(),
                    latency_ms=o.latency_ms,
                    status=o.status,
                    approve=v.approve if v else None,
                    confidence=v.confidence if v else None,
                    reason=v.reason if v else None,
                    tokens_in=tin,
                    tokens_out=tout,
                    cost_usd=cost,
                )
            )
            s.commit()

    # ------------------------------------------------------------ end of day
    async def post_mortem(self, day: dt.date, stats: dict[str, Any]) -> str | None:
        if not self.enabled:
            return None
        user = redact(json.dumps(stats, sort_keys=True, default=str))
        for n in (1, 2):
            p = self.providers.get(n)
            if p is None:
                continue
            slot = self._slot(n)
            try:
                text, _, _ = await asyncio.wait_for(
                    p.complete(REVIEW_SYSTEM, user, REVIEW_SCHEMA, 30.0, 600), timeout=35
                )
                entry = str(json.loads(text)["entry"])[:2000]
            except Exception as e:
                log.warning("AI review slot %d failed: %s", n, type(e).__name__)
                continue
            with Session(self.engine) as s:
                row = s.scalar(select(JournalEOD).where(JournalEOD.trading_date == day))
                if row is not None:
                    row.ai_review, row.ai_provider = entry, f"{p.name}:{slot.model}"
                    s.commit()
            return entry
        return None


def what_if(engine: Engine, book_prefix: str = "SHADOW:") -> dict[str, dict[str, object]]:
    """How trades turned out, grouped by the AI's verdict on their signal.

    Uses the shadow books (every signal a strategy took), so the comparison does not
    depend on shared slots. Signals without an OK verdict count as "no verdict"."""
    with Session(engine) as s:
        rows = s.execute(
            select(Trade.net_pnl, AIVerdict.approve)
            .join(Position, Position.position_uid == Trade.position_uid)
            .join(SignalRow, SignalRow.signal_uid == Position.signal_uid)
            .outerjoin(
                AIVerdict,
                (AIVerdict.signal_uid == SignalRow.signal_uid) & (AIVerdict.status == "OK"),
            )
            .where(Trade.book.like(f"{book_prefix}%"))
        ).all()
    out: dict[str, dict[str, object]] = {}
    for label in ("approved", "disapproved", "no verdict"):
        pnl = [
            r[0]
            for r in rows
            if (label == "approved" and r[1] is True)
            or (label == "disapproved" and r[1] is False)
            or (label == "no verdict" and r[1] is None)
        ]
        out[label] = {
            "trades": len(pnl),
            "net_pnl": str(sum(pnl, Decimal(0))),
            "win_rate": round(sum(1 for x in pnl if x > 0) / len(pnl), 3) if pnl else None,
        }
    return out
