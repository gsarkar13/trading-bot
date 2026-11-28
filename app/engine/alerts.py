"""Alert bus: every alert is written to `event_log` and queued for the notifiers
(Telegram, dashboard). Secrets are scrubbed before anything is stored or sent (inv 18).

Delivery never blocks the trading loop: `emit` only appends to an in-memory queue that
the notifier task drains. Critical alerts are also kept until a notifier confirms them.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.scrub import redact
from app.db.models import EventLog

log = logging.getLogger(__name__)
Level = Literal["info", "warn", "critical"]


@dataclass(frozen=True)
class AlertMsg:
    at: dt.datetime
    level: Level
    category: str
    message: str
    data: dict[str, object] = field(default_factory=dict)


class AlertBus:
    def __init__(
        self,
        engine: Engine | None,
        instance_id: str,
        now: Callable[[], dt.datetime],
        max_queue: int = 500,
    ) -> None:
        self.engine, self.instance_id, self.now = engine, instance_id, now
        self.outbox: deque[AlertMsg] = deque(maxlen=max_queue)
        self.recent: deque[AlertMsg] = deque(maxlen=200)
        self._subs: list[Callable[[AlertMsg], None]] = []

    def subscribe(self, fn: Callable[[AlertMsg], None]) -> None:
        self._subs.append(fn)

    def emit(
        self, level: Level, category: str, message: str, data: dict[str, object] | None = None
    ) -> AlertMsg:
        msg = AlertMsg(self.now(), level, category, redact(message), data or {})
        self.outbox.append(msg)
        self.recent.append(msg)
        if self.engine is not None:
            try:
                with Session(self.engine) as s:
                    s.add(
                        EventLog(
                            at=msg.at,
                            category=category,
                            severity=level,
                            instance_id=self.instance_id,
                            message=msg.message,
                            data=msg.data,
                        )
                    )
                    s.commit()
            except Exception:  # an alert must never break the caller
                log.exception("could not store alert")
        for fn in self._subs:
            try:
                fn(msg)
            except Exception:
                log.exception("alert subscriber failed")
        (log.critical if level == "critical" else log.warning if level == "warn" else log.info)(
            "%s %s: %s", level.upper(), category, msg.message
        )
        return msg

    def drain(self) -> list[AlertMsg]:
        out = list(self.outbox)
        self.outbox.clear()
        return out
