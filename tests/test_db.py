import datetime as dt
from decimal import Decimal

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session

from app.db.models import Base, Candle

SECTION_19_TABLES = {
    "instruments",
    "candles",
    "ticks",
    "option_chain_snapshots",
    "strategies",
    "strategy_gate_reports",
    "signals",
    "ai_verdicts",
    "orders",
    "order_events",
    "fills",
    "semi_tasks",
    "positions",
    "holdings_snapshots",
    "trades",
    "risk_state",
    "arming_sessions",
    "login_sessions",
    "config_versions",
    "event_log",
    "journal_eod",
    "variant_results",
    "instances",
}


def test_migration_creates_every_section_19_table(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names())
    missing = SECTION_19_TABLES - tables
    assert not missing, missing
    assert "strategy_history" in tables  # versioning of the strategies table


def test_models_match_migrations(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], diff


def test_sqlite_is_in_wal_mode(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def _candle(ts: dt.datetime, close: object = Decimal("24000.05")) -> Candle:
    return Candle(
        instrument_key="INDEX|NIFTY",
        timeframe="1m",
        ts=ts,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=close,
        source="fyers",
    )


def test_decimal_and_utc_round_trip(migrated_engine: Engine) -> None:
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    with Session(migrated_engine) as s:
        s.add(
            _candle(
                dt.datetime(2026, 11, 27, 9, 15, tzinfo=ist), Decimal("24123.45000000000000001")
            )
        )
        s.commit()
    with Session(migrated_engine) as s:
        c = s.scalars(select(Candle)).one()
        assert c.close == Decimal("24123.45000000000000001")  # exact, no float rounding
        assert c.ts == dt.datetime(2026, 11, 27, 3, 45, tzinfo=dt.UTC)


def test_naive_datetime_refused(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as s:
        s.add(_candle(dt.datetime(2026, 11, 27, 9, 15)))  # noqa: DTZ001
        with pytest.raises(StatementError, match="naive"):
            s.commit()


def test_float_money_refused(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as s:
        s.add(_candle(dt.datetime(2026, 11, 27, 3, 45, tzinfo=dt.UTC), 24000.05))
        with pytest.raises(StatementError, match="float"):
            s.commit()
