"""Keep the `strategies` table in step with the seed rows and the plugin registry.

* Missing rows are inserted from `SEED_ROWS` (with a `strategy_history` entry).
* For existing rows, only `code_status` is refreshed: it is a cached display value,
  always recomputed from the plugin registry (DECISIONS.md D6). The trader's own edits
  (priority, active, requested mode, notes, stage) are never overwritten.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import StrategyHistory, StrategyRow
from app.strategies.registry import SEED_ROWS, code_status


def sync_strategies_table(session: Session, now: dt.datetime) -> list[str]:
    """Return the ids of rows inserted. Caller commits."""
    existing = {r.id: r for r in session.scalars(select(StrategyRow))}
    inserted = []
    for seed in SEED_ROWS:
        row = existing.get(seed.id)
        status = code_status(seed.id).value
        if row is None:
            row = StrategyRow(
                id=seed.id,
                priority=seed.priority,
                name=seed.name,
                segment=seed.segment.value,
                underlyings=",".join(seed.underlyings),
                stage=seed.stage.value,
                code_status=status,
                active=False,
                requested_mode="PAPER",
                gate_status="not_run",
                notes=seed.notes,
                version=1,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            session.add(
                StrategyHistory(
                    strategy_id=seed.id,
                    version=1,
                    changed_at=now,
                    changed_by="seed",
                    reason="initial seed row",
                    snapshot={
                        "priority": seed.priority,
                        "stage": seed.stage.value,
                        "code_status": status,
                        "active": False,
                        "requested_mode": "PAPER",
                    },
                )
            )
            inserted.append(seed.id)
        elif row.code_status != status:
            row.code_status = status
            row.updated_at = now
            # A placeholder can never be active (section 6.2).
            if status != "implemented":
                row.active = False
    return inserted
