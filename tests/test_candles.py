import datetime as dt
from decimal import Decimal

from sqlalchemy import Engine

from app.adapters.base import TickData
from app.core.types import InstrumentKey
from app.data.bars import Bar
from app.data.candle_builder import CandleBuilder
from app.data.resample import daily, resample
from app.data.store import load_bars, upsert_bars
from tests.helpers import bar, ist, session_bars

D = dt.date(2026, 11, 30)
K = InstrumentKey.future("NIFTY", dt.date(2026, 12, 29))


def tick(h: int, m: int, s: int, px: float, cum: int | None = None) -> TickData:
    ts = ist(D, h, m) + dt.timedelta(seconds=s)
    return TickData(key=K, exchange_ts=ts, received_ts=ts, ltp=Decimal(str(px)), cum_volume=cum)


def test_builds_ohlcv_from_exchange_time_with_grace() -> None:
    cb = CandleBuilder(grace_s=2)
    out = []
    for t in [
        tick(9, 15, 0, 100, 10),
        tick(9, 15, 20, 103, 25),
        tick(9, 15, 40, 99, 30),
        tick(9, 15, 59, 101, 40),
        tick(9, 16, 1, 102, 50),
    ]:
        out += cb.add(t)
    assert out == []  # 09:15 not closed until 09:16:02
    out += cb.add(tick(9, 16, 2, 102.5, 55))
    assert out == [Bar(ist(D, 9, 15), 100, 103, 99, 101, 30)]  # first minute: 40 - 10
    out = cb.add(tick(9, 17, 5, 104, 80)) + cb.flush()
    assert [b.volume for b in out] == [15, 25]  # 55-40, 80-55


def test_duplicates_dropped_out_of_order_within_minute_kept_late_dropped() -> None:
    cb = CandleBuilder()
    cb.add(tick(9, 15, 10, 100, 10))
    cb.add(tick(9, 15, 10, 100, 10))  # exact duplicate
    cb.add(tick(9, 15, 5, 98, 8))  # out of order but same open minute: kept
    closed = cb.add(tick(9, 16, 3, 101, 20))
    assert closed[0].low == 98 and cb.stats.duplicates == 1
    cb.add(tick(9, 15, 30, 50, 21))  # late tick for an emitted minute
    assert cb.stats.late == 1
    assert cb.flush()[0].low == 101  # the late 50 never touched any candle


def test_gap_minutes_recorded_and_volume_reset_guarded() -> None:
    cb = CandleBuilder()
    bars = cb.add(tick(9, 15, 1, 100, 100))
    bars += cb.add(tick(9, 18, 1, 101, 50))  # 3 minutes later, cum volume went DOWN
    bars += cb.flush()
    assert [b.ts for b in bars] == [ist(D, 9, 15), ist(D, 9, 18)]
    assert cb.stats.gap_minutes == [ist(D, 9, 16), ist(D, 9, 17)]
    assert bars[1].volume == 0 and cb.stats.volume_resets == 1


def test_index_ticks_have_zero_volume() -> None:
    cb = CandleBuilder()
    cb.add(tick(9, 15, 1, 100))
    assert cb.flush()[0].volume == 0


def test_15m_resample_is_session_aligned_and_flags_partial() -> None:
    bars = session_bars(D, 24000, 7, n=40)  # 09:15 .. 09:54
    out = list(resample(bars, 15))
    assert [b.ts for b, _ in out] == [ist(D, 9, 15), ist(D, 9, 30), ist(D, 9, 45)]
    assert [c for _, c in out] == [True, True, False]  # 09:45 bucket has only 10 minutes
    first = out[0][0]
    assert first.open == bars[0].open and first.close == bars[14].close
    assert first.high == max(b.high for b in bars[:15])
    assert first.volume == sum(b.volume for b in bars[:15])
    full = list(resample(session_bars(D, 24000, 7, n=375), 15))
    assert len(full) == 25 and all(c for _, c in full)


def test_daily_bars_group_by_ist_date() -> None:
    d2 = D + dt.timedelta(days=1)
    bars = session_bars(D, 100, 1, n=5) + session_bars(d2, 200, 2, n=5)
    days = daily(bars)
    assert [b.ts for b in days] == [ist(D, 9, 15), ist(d2, 9, 15)]
    assert days[1].open == bars[5].open


def test_store_round_trip_ignores_duplicates(migrated_engine: Engine) -> None:
    bars = session_bars(D, 24000, 3, n=30)
    assert upsert_bars(migrated_engine, K.canonical, "1m", "fyers_hist", bars) == 30
    upsert_bars(migrated_engine, K.canonical, "1m", "fyers_hist", bars)  # re-run is safe
    upsert_bars(migrated_engine, K.canonical, "1m", "fyers_ws", [bar(bars[0].ts, 1, 1, 1, 1)])
    got = load_bars(migrated_engine, K.canonical, "1m", ["fyers_hist", "fyers_ws"], D, D)
    assert len(got) == 30 and got[0].open == bars[0].open  # hist preferred over ws
    assert got[5] == bars[5]
    ws_only = load_bars(migrated_engine, K.canonical, "1m", "fyers_ws")
    assert len(ws_only) == 1
