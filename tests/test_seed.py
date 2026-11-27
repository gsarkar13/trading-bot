import datetime as dt

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.db.models import StrategyHistory, StrategyRow
from app.db.seed import sync_strategies_table
from app.strategies import registry
from tests.test_strategies_table import _Dummy

NOW = dt.datetime(2026, 11, 27, 4, 0, tzinfo=dt.UTC)


def test_seed_inserts_all_rows_once(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as s:
        assert len(sync_strategies_table(s, NOW)) == 11
        s.commit()
        assert sync_strategies_table(s, NOW) == []  # idempotent
        rows = s.scalars(select(StrategyRow).order_by(StrategyRow.priority)).all()
        assert [r.priority for r in rows] == list(range(1, 12))
        assert all(not r.active and r.requested_mode == "PAPER" for r in rows)
        assert len(s.scalars(select(StrategyHistory)).all()) == 11


def test_seed_keeps_trader_edits_and_refreshes_code_status(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", {})
    with Session(migrated_engine) as s:
        sync_strategies_table(s, NOW)
        row = s.get(StrategyRow, "pdh_pdl_breakout")
        assert row is not None and row.code_status == "placeholder"
        row.priority = 7  # trader re-ranked it
        s.commit()
        registry.register(_Dummy)  # plugin now exists
        sync_strategies_table(s, NOW)
        s.commit()
        row = s.get(StrategyRow, "pdh_pdl_breakout")
        assert row is not None and row.code_status == "implemented" and row.priority == 7
