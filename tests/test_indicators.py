import datetime as dt

import pandas as pd
import pytest

from app.core.timeutil import to_ist
from app.data.bars import Bar
from app.indicators.core import (
    ATR,
    EMA,
    RVOLSma,
    RVOLTimeOfDay,
    is_hammer,
    is_shooting_star,
)
from tests.helpers import bar, ist, session_bars, trading_days

D = dt.date(2026, 11, 2)


def _two_sessions() -> list[Bar]:
    days = trading_days(D, 2)
    return session_bars(days[0], 24000, 1) + session_bars(days[1], 24150, 2)  # gap up


def test_ema_matches_pandas_ewm_adjust_false() -> None:
    closes = [b.close for b in _two_sessions()]
    ref = pd.Series(closes).ewm(span=200, adjust=False).mean().tolist()
    ema = EMA(200)
    got = [ema.update(c) for c in closes]
    assert got == pytest.approx(ref, rel=1e-12)


def test_ema_warmup_is_three_times_period() -> None:
    ema = EMA(200)
    for i in range(599):
        ema.update(100.0 + i)
    assert not ema.ready
    ema.update(1.0)
    assert ema.ready and ema.warmup_bars == 600


def test_atr_matches_pandas_with_session_reset() -> None:
    bars = _two_sessions()
    df = pd.DataFrame(
        [{"h": b.high, "l": b.low, "c": b.close, "d": to_ist(b.ts).date()} for b in bars]
    )
    prev = df["c"].shift(1).where(df["d"] == df["d"].shift(1))  # D15: no cross-session close
    tr = pd.concat([df["h"] - df["l"], (df["h"] - prev).abs(), (df["l"] - prev).abs()], axis=1).max(
        axis=1
    )
    ref = tr.ewm(alpha=1 / 14, adjust=False).mean().tolist()
    atr = ATR(14)
    got = [atr.update(b) for b in bars]
    assert got == pytest.approx(ref, rel=1e-12)


def test_atr_first_bar_of_day_ignores_overnight_gap() -> None:
    a = ATR(14)
    a.update(bar(ist(D, 15, 29), 100, 101, 99, 100))
    nxt = D + dt.timedelta(days=1)
    gap_bar = bar(ist(nxt, 9, 15), 150, 151, 149, 150)  # 50-point gap
    assert ATR.true_range(gap_bar, None) == 2
    v = a.update(gap_bar)
    assert v == pytest.approx(2.0)  # TR=2 both bars; gap ignored


def test_rvol_sma_excludes_current_bar() -> None:
    r = RVOLSma(20)
    for _ in range(20):
        assert r.value_for(100) is None or r.ready
        r.add(100)
    assert r.value_for(250) == pytest.approx(2.5)
    r.reset()
    assert r.value_for(250) is None


def test_rvol_time_of_day_uses_same_minute() -> None:
    r = RVOLTimeOfDay(sessions=3)
    days = trading_days(D, 4)
    for d in days[:3]:
        r.add(bar(ist(d, 9, 15), 1, 1, 1, 1, v=1000))  # big opening minute
        r.add(bar(ist(d, 12, 0), 1, 1, 1, 1, v=100))  # quiet midday
    assert r.value_for(bar(ist(days[3], 12, 0), 1, 1, 1, 1, v=250)) == pytest.approx(2.5)
    assert r.value_for(bar(ist(days[3], 9, 15), 1, 1, 1, 1, v=1000)) == pytest.approx(1.0)
    assert r.value_for(bar(ist(days[3], 10, 0), 1, 1, 1, 1, v=100)) is None  # no history


T = ist(D, 10, 0)


@pytest.mark.parametrize(
    ("o", "h", "low", "c", "expect_hammer", "expect_star"),
    [
        (100, 101, 90, 100.5, True, False),  # classic hammer
        (100, 100, 90, 100, True, False),  # doji-hammer: body = 1 tick, no div-by-zero
        (100, 110, 99.5, 100.5, False, True),  # shooting star
        (100, 100, 100, 100, False, False),  # zero range
        (100, 104, 96, 101, False, False),  # ordinary candle
        (95, 101, 90, 100, False, False),  # body too big for the wick
        (100, 105, 90, 100.5, False, False),  # upper wick 4.5 > 0.25 x range 15 = 3.75
        (92, 101, 90, 93, False, True),  # long upper wick, close in bottom third: a star
    ],
)
def test_patterns(
    o: float, h: float, low: float, c: float, expect_hammer: bool, expect_star: bool
) -> None:
    b = bar(T, o, h, low, c)
    assert is_hammer(b, atr=5, min_range_atr=0.8) is expect_hammer
    assert is_shooting_star(b, atr=5, min_range_atr=0.8) is expect_star


def test_tiny_candle_filtered_by_min_range_atr() -> None:
    tiny = bar(T, 100, 100.1, 99.0, 100.05)  # perfect shape, range 1.1
    assert is_hammer(tiny, atr=1, min_range_atr=0.8)
    assert not is_hammer(tiny, atr=5, min_range_atr=0.8)  # 1.1 < 4.0


def test_bar_rejects_bad_ohlc_and_naive_time() -> None:
    with pytest.raises(ValueError):
        bar(T, 100, 99, 98, 100)
    with pytest.raises(ValueError):
        Bar(dt.datetime(2026, 1, 1), 1, 1, 1, 1)  # noqa: DTZ001


def test_rvol_time_of_day_ready_despite_odd_minutes() -> None:
    r = RVOLTimeOfDay(sessions=3)
    days = trading_days(D, 5)
    r.add(bar(ist(days[0], 15, 30), 1, 1, 1, 1, v=5))  # one stray post-close candle
    for d in days[:3]:
        r.add(bar(ist(d, 10, 0), 1, 1, 1, 1, v=100))
    assert r.ready  # three sessions seen
    assert r.value_for(bar(ist(days[3], 10, 0), 1, 1, 1, 1, v=300)) == pytest.approx(3.0)
    assert r.value_for(bar(ist(days[3], 15, 30), 1, 1, 1, 1, v=5)) is None  # thin slot
