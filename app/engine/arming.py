"""Daily arming of real-money trading (brief section 12, invariant 10).

Two steps: `request()` returns a fresh 4-digit code (sent to the trader by Telegram or
shown on the dashboard); `confirm(code)` within 2 minutes arms real money for TODAY.
Arming lasts until the end of the trading day or until this process stops, whichever is
first: it is tied to `process_start_id`, so a restart always comes back unarmed, in PAPER.

Honest limit: if the code goes to the same Telegram chat that confirms it, this step
stops a mis-tap, not someone holding your phone (ARCHITECTURE.md review point 7).
"""

from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.timeutil import IST, to_ist
from app.db.models import ArmingSession, EventLog

CODE_TTL_S = 120


@dataclass
class _Pending:
    code: str
    at: dt.datetime
    via: str


class Arming:
    def __init__(
        self,
        engine: Engine,
        instance_id: str,
        process_start_id: str,
        session_close: dt.time = dt.time(15, 30),
    ) -> None:
        self.engine, self.instance_id, self.process_id = engine, instance_id, process_start_id
        self.session_close = session_close
        self._pending: _Pending | None = None
        self._failed = 0

    def request(self, now: dt.datetime, via: str) -> str:
        code = f"{secrets.randbelow(10_000):04d}"
        self._pending = _Pending(code, now, via)
        return code

    def confirm(self, now: dt.datetime, code: str, via: str, summary: dict[str, object]) -> bool:
        p = self._pending
        if p is None or (now - p.at).total_seconds() > CODE_TTL_S:
            self._pending = None
            return False
        if not secrets.compare_digest(code.strip(), p.code):
            self._failed += 1
            if self._failed >= 3:  # three wrong codes: start over
                self._pending, self._failed = None, 0
            return False
        self._pending, self._failed = None, 0
        with Session(self.engine) as s:
            s.add(
                ArmingSession(
                    instance_id=self.instance_id,
                    process_start_id=self.process_id,
                    trading_date=to_ist(now).date(),
                    armed_at=now,
                    armed_via=via,
                    summary=summary,
                )
            )
            s.add(
                EventLog(
                    at=now,
                    category="RISK",
                    severity="critical",
                    instance_id=self.instance_id,
                    message=f"REAL MONEY ARMED for today via {via}",
                    data=summary,
                )
            )
            s.commit()
        return True

    def disarm(self, now: dt.datetime, reason: str) -> None:
        with Session(self.engine) as s:
            for a in s.scalars(
                select(ArmingSession).where(
                    ArmingSession.process_start_id == self.process_id,
                    ArmingSession.disarmed_at.is_(None),
                )
            ):
                a.disarmed_at, a.disarm_reason = now, reason
            s.add(
                EventLog(
                    at=now,
                    category="RISK",
                    severity="warn",
                    instance_id=self.instance_id,
                    message=f"disarmed: {reason}",
                )
            )
            s.commit()

    def is_armed(self, now: dt.datetime) -> bool:
        local = to_ist(now)
        if local.time() >= self.session_close:
            return False  # ends with the trading day
        with Session(self.engine) as s:
            row = s.scalar(
                select(ArmingSession).where(
                    ArmingSession.process_start_id == self.process_id,
                    ArmingSession.trading_date == local.date(),
                    ArmingSession.disarmed_at.is_(None),
                )
            )
        return row is not None


def ist_today(now: dt.datetime) -> dt.date:
    return now.astimezone(IST).date()
