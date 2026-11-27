"""Column types that keep money exact and timestamps in UTC (brief section 14.4)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import Dialect, Numeric, String
from sqlalchemy.types import DateTime, TypeDecorator, TypeEngine


class UTCDateTime(TypeDecorator[dt.datetime]):
    """Stores naive UTC; refuses naive input; returns aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime given to a UTC column; attach a timezone")
        return value.astimezone(dt.UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        assert isinstance(value, dt.datetime)
        return value.replace(tzinfo=dt.UTC)


class ExactDecimal(TypeDecorator[Decimal]):
    """Exact decimal: TEXT on SQLite (which has no decimal type), NUMERIC elsewhere."""

    impl = String(40)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(40))
        return dialect.type_descriptor(Numeric(20, 6))

    def process_bind_param(self, value: Decimal | int | str | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError("floats are not allowed for money/price columns; use Decimal")
        d = Decimal(str(value))
        return format(d, "f") if dialect.name == "sqlite" else d

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(value))
