import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.types import InstrumentKey, Mode, OptionType


def test_option_key_canonical_is_stable() -> None:
    k = InstrumentKey.option("NIFTY", dt.date(2026, 12, 1), 24000, OptionType.CE)
    assert k.canonical == "OPT|NIFTY|2026-12-01|24000|CE"
    assert InstrumentKey.option("NIFTY", dt.date(2026, 12, 1), "24000.00", OptionType.CE) == k


def test_fractional_strike_kept() -> None:
    k = InstrumentKey.option("X", dt.date(2026, 12, 1), "75.5", OptionType.PE)
    assert k.canonical.endswith("|75.5|PE")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "OPT", "underlying": "NIFTY", "expiry": dt.date(2026, 12, 1)},  # no strike
        {"kind": "FUT", "underlying": "NIFTY"},  # no expiry
        {"kind": "EQ", "isin": "INE002A01018"},  # no series
        {"kind": "EQ", "isin": "INE002A01018", "series": "EQ", "underlying": "RELIANCE"},
        {"kind": "INDEX", "underlying": "nifty"},  # lower case
        {
            "kind": "OPT",
            "underlying": "NIFTY",
            "expiry": dt.date(2026, 12, 1),
            "strike": Decimal("-1"),
            "option_type": OptionType.CE,
        },
    ],
)
def test_bad_keys_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        InstrumentKey(**kwargs)  # type: ignore[arg-type]


def test_keys_are_hashable_and_frozen() -> None:
    k = InstrumentKey.future("BANKNIFTY", dt.date(2026, 12, 29))
    assert {k: 1}[InstrumentKey.future("BANKNIFTY", dt.date(2026, 12, 29))] == 1
    with pytest.raises(ValidationError):
        k.underlying = "NIFTY"  # type: ignore[misc]


def test_mode_ordering() -> None:
    assert Mode.lowest(Mode.LIVE, Mode.SEMI) is Mode.SEMI
    assert Mode.lowest(Mode.LIVE, Mode.PAPER, Mode.SEMI) is Mode.PAPER
    assert not Mode.PAPER.is_real_money and Mode.SEMI.is_real_money
