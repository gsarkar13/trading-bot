"""Changes to the strategies table from the dashboard or Telegram (sections 6.2, inv 17).

Every change is versioned in `strategy_history` and logged as a configuration event.
Invariant 17: a strategy with open positions or working orders can't be switched off and
can't change mode; close the positions first, or use flatten. Only implemented strategies
can be switched on (D6).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.types import Mode
from app.db.models import EventLog, StrategyHistory, StrategyRow
from app.strategies.registry import can_activate


class ChangeRefused(Exception):
    pass


class StrategyTable:
    def __init__(self, engine: Engine, is_exposed: Callable[[str], bool], instance_id: str) -> None:
        self.engine, self.is_exposed, self.instance_id = engine, is_exposed, instance_id

    def rows(self) -> list[StrategyRow]:
        with Session(self.engine) as s:
            return list(s.scalars(select(StrategyRow).order_by(StrategyRow.priority)))

    def _change(
        self,
        sid: str,
        now: dt.datetime,
        by: str,
        what: str,
        apply: Callable[[Session, StrategyRow], None],
    ) -> StrategyRow:
        with Session(self.engine) as s:
            row = s.get(StrategyRow, sid)
            if row is None:
                raise ChangeRefused(f"unknown strategy {sid}")
            apply(s, row)
            row.version += 1
            row.updated_at = now
            s.add(
                StrategyHistory(
                    strategy_id=sid,
                    version=row.version,
                    changed_at=now,
                    changed_by=by,
                    reason=what,
                    snapshot={
                        "priority": row.priority,
                        "active": row.active,
                        "requested_mode": row.requested_mode,
                        "stage": row.stage,
                    },
                )
            )
            s.add(
                EventLog(
                    at=now,
                    category="RISK",
                    severity="info",
                    instance_id=self.instance_id,
                    message=f"{sid}: {what} (by {by})",
                )
            )
            s.commit()
            s.refresh(row)
            s.expunge(row)
            return row

    def set_active(self, sid: str, active: bool, now: dt.datetime, by: str) -> StrategyRow:
        if active:
            ok, why = can_activate(sid)
            if not ok:
                raise ChangeRefused(f"{sid} can't be switched on: {why}")
        elif self.is_exposed(sid):
            raise ChangeRefused(f"{sid} has open positions or orders; close them or flatten first")

        def apply(s: Session, r: StrategyRow) -> None:
            r.active = active

        return self._change(sid, now, by, f"active={active}", apply)

    def set_mode(self, sid: str, mode: Mode, now: dt.datetime, by: str) -> StrategyRow:
        if self.is_exposed(sid):
            raise ChangeRefused(f"{sid} has open positions or orders; mode can't change now")

        def apply(s: Session, r: StrategyRow) -> None:
            r.requested_mode = mode.value

        return self._change(sid, now, by, f"requested_mode={mode.value}", apply)

    def move(self, sid: str, up: bool, now: dt.datetime, by: str) -> StrategyRow:
        """Swap priority with the neighbour above (up) or below. Allowed while exposed:
        it only affects future signals."""
        with Session(self.engine) as s:
            rows = list(s.scalars(select(StrategyRow).order_by(StrategyRow.priority)))
        ids = [r.id for r in rows]
        if sid not in ids:
            raise ChangeRefused(f"unknown strategy {sid}")
        i = ids.index(sid)
        j = i - 1 if up else i + 1
        if not 0 <= j < len(ids):
            raise ChangeRefused("already at the end")
        other = ids[j]
        pi, pj = rows[i].priority, rows[j].priority

        def to(p: int) -> Callable[[Session, StrategyRow], None]:
            def apply(s: Session, r: StrategyRow) -> None:
                r.priority = p

            return apply

        self._change(other, now, by, f"priority {pj}->{pi}", to(pi))
        return self._change(sid, now, by, f"priority {pi}->{pj}", to(pj))
