import datetime as dt
from decimal import Decimal

import pytest

from app.config.loader import REPO_ROOT
from app.core.timeutil import IST
from app.research.contracts import ExpiryCalendar, load_specs
from app.research.costs import ChargesTable
from app.research.options import bs_delta, bs_price, pick_strike, years_to_expiry

SPECS = load_specs(REPO_ROOT / "config" / "contracts.yaml")


def test_black_scholes_known_value_and_parity() -> None:
    # Hull's textbook example: S=42, K=40, r=10%, vol=20%, T=0.5 -> call 4.76, put 0.81
    assert bs_price(42, 40, 0.5, 0.2, 0.1, True) == pytest.approx(4.76, abs=0.005)
    assert bs_price(42, 40, 0.5, 0.2, 0.1, False) == pytest.approx(0.81, abs=0.005)
    import math

    c, p = (
        bs_price(24000, 24100, 0.02, 0.13, 0.065, True),
        bs_price(24000, 24100, 0.02, 0.13, 0.065, False),
    )
    assert c - p == pytest.approx(24000 - 24100 * math.exp(-0.065 * 0.02), abs=1e-6)
    assert 0.45 < bs_delta(24000, 24000, 0.02, 0.13, 0.065, True) < 0.55


def test_time_to_expiry_floor_and_strikes() -> None:
    exp = dt.date(2026, 12, 1)
    assert years_to_expiry(dt.datetime(2026, 12, 1, 15, 40, tzinfo=IST), exp) > 0
    one_day = years_to_expiry(dt.datetime(2026, 11, 30, 15, 30, tzinfo=IST), exp)
    assert one_day == pytest.approx(1 / 365)
    assert pick_strike(24024, 50, "ATM", True) == 24000
    assert pick_strike(24024, 50, "ITM_1", True) == 23950
    assert pick_strike(24024, 50, "ITM_1", False) == 24050
    k = pick_strike(24000, 50, "delta_target", True, t=0.02, vol=0.13, r=0.065, delta_target=0.3)
    assert k > 24000


def test_expiry_calendar_rules_and_holiday_shift() -> None:
    nifty = SPECS["NIFTY"]
    days = [dt.date(2027, 1, 1) + dt.timedelta(days=i) for i in range(40)]
    open_days = [d for d in days if d.weekday() < 5 and d != dt.date(2027, 1, 26)]
    cal = ExpiryCalendar(nifty, open_days)
    assert cal.monthly(2027, 1) == dt.date(2027, 1, 25)  # same as the real Fyers master
    assert cal.next_weekly(dt.date(2027, 1, 20)) == dt.date(2027, 1, 25)
    assert cal.next_weekly(dt.date(2027, 1, 13)) == dt.date(2027, 1, 19)
    assert cal.trading_expiry(dt.date(2027, 1, 19), "next_weekly") == dt.date(2027, 1, 25)
    assert ExpiryCalendar(SPECS["BANKNIFTY"], open_days).trading_expiry(
        dt.date(2027, 1, 5), "next_weekly"
    ) == dt.date(2027, 1, 25)
    old = ExpiryCalendar(nifty, [])
    assert old.monthly(2025, 3).weekday() == 3  # Thursday era (unverified cut-over)


def test_lot_size_by_date() -> None:
    assert SPECS["NIFTY"].lot_size(dt.date(2026, 6, 1))[0] == 65
    assert SPECS["NIFTY"].lot_size(dt.date(2025, 6, 1))[0] == 75


def test_option_charges_hand_computed() -> None:
    ct = ChargesTable(REPO_ROOT / "config" / "charges.yaml")
    buy, sell = Decimal("7800"), Decimal("8450")  # 65 x 120 in, 65 x 130 out
    c = ct.option_round_trip(dt.date(2026, 11, 30), "flattrade", buy, sell)
    assert c["brokerage"] == 0
    assert c["stt"] == Decimal("12.68")  # 0.15% of 8450
    assert c["exchange_txn"] == Decimal("5.77")  # 0.03553% of 16250
    assert c["stamp"] == Decimal("0.23")  # 0.003% of 7800
    assert c["gst"] == Decimal("1.04")  # 18% of (0 + 5.774 + 0.016)
    assert c["total"] == sum(v for k, v in c.items() if k != "total")
    fy = ct.option_round_trip(dt.date(2026, 11, 30), "fyers", buy, sell)
    assert fy["brokerage"] == Decimal(40) and fy["total"] > c["total"]
