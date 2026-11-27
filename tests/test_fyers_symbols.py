"""Symbol mapping on real Fyers master excerpts (tests/fixtures/fyers/*, public files,
downloaded 2026-11-27)."""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from app.adapters.base import InstrumentMappingError
from app.adapters.fyers.symbols import SymbolMap, parse_master
from app.core.types import InstrumentKey, OptionType

FIX = Path(__file__).parent / "fixtures" / "fyers"


@pytest.fixture(scope="module")
def smap() -> SymbolMap:
    text = (FIX / "NSE_FO_sample.csv").read_text() + (FIX / "NSE_CM_sample.csv").read_text()
    return SymbolMap(parse_master(text))


def test_index_and_vix_keys(smap: SymbolMap) -> None:
    assert smap.symbol(InstrumentKey.index("NIFTY")) == "NSE:NIFTY50-INDEX"
    assert smap.symbol(InstrumentKey.index("BANKNIFTY")) == "NSE:NIFTYBANK-INDEX"
    assert smap.symbol(InstrumentKey.index("INDIAVIX")) == "NSE:INDIAVIX-INDEX"


def test_futures_lot_tick_and_round_trip(smap: SymbolMap) -> None:
    k = InstrumentKey.future("NIFTY", dt.date(2026, 12, 29))
    info = smap.info(k)
    assert info.broker_symbol == "NSE:NIFTY26DECFUT"
    assert (info.lot_size, info.tick_size) == (65, Decimal("0.1"))
    assert smap.key_for("NSE:NIFTY26DECFUT") == k
    assert smap.info(InstrumentKey.future("BANKNIFTY", dt.date(2026, 12, 29))).lot_size == 30


def test_weekly_option_maps_by_key(smap: SymbolMap) -> None:
    k = InstrumentKey.option("NIFTY", dt.date(2026, 12, 1), 24000, OptionType.CE)
    info = smap.info(k)
    assert info.broker_symbol == "NSE:NIFTY26D0124000CE"  # weekly format, read not built
    assert (info.lot_size, info.tick_size) == (65, Decimal("0.05"))
    assert smap.key_for(info.broker_symbol) == k


def test_expiries_come_from_master_including_holiday_shift(smap: SymbolMap) -> None:
    fut = smap.future_expiries("NIFTY")
    assert dt.date(2027, 1, 25) in fut  # Monday: Tue 26 Jan 2027 is Republic Day
    assert all(e.weekday() == 1 for e in smap.option_expiries("NIFTY") if e != dt.date(2027, 1, 25))
    assert smap.option_expiries("BANKNIFTY")  # monthly only
    assert all(e.day >= 22 for e in smap.option_expiries("BANKNIFTY"))


def test_strike_interval_and_atm(smap: SymbolMap) -> None:
    e = dt.date(2026, 12, 1)
    assert smap.strike_interval("NIFTY", e) == Decimal(50)
    assert smap.atm_strike("NIFTY", e, 24024.0) == Decimal(24000)
    assert smap.atm_strike("NIFTY", e, 24026.0) == Decimal(24050)
    assert smap.strike_interval("BANKNIFTY", dt.date(2026, 12, 29)) == Decimal(100)


def test_front_future_rolls_before_expiry(smap: SymbolMap) -> None:
    # Rolls when fewer than N trading days remain until expiry (29 Dec 2026).
    assert smap.front_future("NIFTY", dt.date(2026, 12, 23), 3).expiry == dt.date(2026, 12, 29)
    assert smap.front_future("NIFTY", dt.date(2026, 12, 28), 3).expiry == dt.date(2027, 1, 25)

    def holiday_aware(a: dt.date, b: dt.date) -> int:  # Christmas is a holiday
        return sum(
            1
            for i in range(1, (b - a).days + 1)
            if (d := a + dt.timedelta(days=i)).weekday() < 5 and d != dt.date(2026, 12, 25)
        )

    assert smap.front_future("NIFTY", dt.date(2026, 12, 24), 3, holiday_aware).expiry == dt.date(
        2027, 1, 25
    )


def test_equity_by_isin(smap: SymbolMap) -> None:
    assert smap.symbol(InstrumentKey.equity("INE002A01018")) == "NSE:RELIANCE-EQ"


def test_unknown_key_fails_closed(smap: SymbolMap) -> None:
    with pytest.raises(InstrumentMappingError):
        smap.symbol(InstrumentKey.option("NIFTY", dt.date(2026, 12, 2), 24000, OptionType.CE))
    with pytest.raises(InstrumentMappingError):
        smap.key_for("NSE:NOSUCH-EQ")
