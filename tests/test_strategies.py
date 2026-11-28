import datetime as dt
from decimal import Decimal

import pytest

from app.config.schema import Ema200HammerParams, PdhPdlParams
from app.data.bars import Bar
from app.strategies.base import MarketView, OIPulse, OpenPosition
from app.strategies.common import Aggregator, oi_confirms
from app.strategies.ema200_hammer import Ema200HammerRvolOi, trail_exit
from app.strategies.oi import ChainPoint, OIPulseTracker
from app.strategies.pdh_pdl import PdhPdlBreakout
from tests.helpers import bar, ist, trading_days

M = dt.timedelta(minutes=1)


def trend_sessions(days: list[dt.date], drift: float, start: float = 24000) -> list[Bar]:
    """Smooth trending 1-minute sessions with small, ordinary candles."""
    out, px = [], start
    for d in days:
        t0 = ist(d, 9, 15)
        for i in range(375):
            o = px
            c = o + drift + (0.5 if i % 2 else -0.5)
            out.append(Bar(t0 + i * M, o, max(o, c) + 0.5, min(o, c) - 0.5, c, 1000))
            px = c
    return out


def run(plugin, bars, fut_vol=None, pulse=None, start_reset=None):  # type: ignore[no-untyped-def]
    sigs = []
    for b in bars:
        vol = fut_vol(b) if fut_vol else 1000
        fb = Bar(b.ts, b.open, b.high, b.low, b.close, vol)
        sigs += plugin.on_candle(
            "1m",
            MarketView(
                underlying="NIFTY",
                now=b.ts + M,
                bar=b,
                futures_bar=fb,
                oi_pulse=pulse,
                volume_series_reset=b.ts == start_reset,
            ),
        )
    return sigs


def hammer_after(plugin: Ema200HammerRvolOi, last: Bar, below_ema: bool = True) -> Bar:
    st = plugin._s["NIFTY"]
    ema, atr = st.ema.value, st.atr.value
    assert ema is not None and atr is not None
    close = ema + 0.5
    return Bar(
        last.ts + M,
        close - 0.2,
        close + 0.1,
        ema - 3 * atr if below_ema else close - 0.3,
        close,
        1000,
    )


DAYS = trading_days(dt.date(2026, 6, 1), 26)  # > 24 sessions: 600 15m bars of warm-up


@pytest.fixture(scope="module")
def warm_up_bars() -> list[Bar]:
    return trend_sessions(DAYS, drift=0.1)


def test_strategy1_fires_on_planted_hammer(warm_up_bars: list[Bar]) -> None:
    p = Ema200HammerRvolOi(Ema200HammerParams(), "h1")
    assert run(p, warm_up_bars) == []  # ordinary candles never qualify
    h = hammer_after(p, warm_up_bars[-1])
    sigs = run(p, [h], fut_vol=lambda b: 5000)  # RVOL 5x
    assert len(sigs) == 1
    s = sigs[0]
    assert s.direction == "LONG_CE" and s.config_hash == "h1"
    assert float(s.stop_underlying) == pytest.approx(h.low - 5, abs=0.01)
    risk = h.close - (h.low - 5)
    assert float(s.target_underlying or 0) == pytest.approx(h.close + 2 * risk, abs=0.02)
    c = s.checklist
    assert c["trend_15m"] == "bull" and c["pattern"] and c["ema_touch"]
    assert c["rvol"] == pytest.approx(5.0) and c["oi"] == "UNKNOWN"


def test_strategy1_needs_rvol(warm_up_bars: list[Bar]) -> None:
    p = Ema200HammerRvolOi(Ema200HammerParams(), "h")
    run(p, warm_up_bars)
    assert run(p, [hammer_after(p, warm_up_bars[-1])], fut_vol=lambda b: 1500) == []  # 1.5x


def test_strategy1_silent_before_warmup(warm_up_bars: list[Bar]) -> None:
    p = Ema200HammerRvolOi(Ema200HammerParams(), "h")
    short = warm_up_bars[: 20 * 375]  # 20 sessions: 15m EMA not ready (needs 600 bars)
    run(p, short)
    assert not p._s["NIFTY"].trend.ready
    assert run(p, [hammer_after(p, short[-1])], fut_vol=lambda b: 9000) == []


def test_strategy1_ignores_bullish_hammer_in_downtrend() -> None:
    bars = trend_sessions(DAYS, drift=-0.1)
    p = Ema200HammerRvolOi(Ema200HammerParams(), "h")
    run(p, bars)
    assert p._s["NIFTY"].trend.direction() == "bear"
    assert run(p, [hammer_after(p, bars[-1])], fut_vol=lambda b: 9000) == []


def test_strategy1_rvol_restarts_after_futures_roll(warm_up_bars: list[Bar]) -> None:
    p = Ema200HammerRvolOi(Ema200HammerParams(), "h")
    run(p, warm_up_bars)
    h = hammer_after(p, warm_up_bars[-1])
    assert run(p, [h], fut_vol=lambda b: 9000, start_reset=h.ts) == []  # lookback empty


def test_strategy1_carries_oi_result(warm_up_bars: list[Bar]) -> None:
    p = Ema200HammerRvolOi(Ema200HammerParams(), "h")
    run(p, warm_up_bars)
    pulse = OIPulse(
        15, put_oi_change=500, call_oi_change=-100, pcr_start=0.9, pcr_end=1.0, age_s=10
    )
    s = run(p, [hammer_after(p, warm_up_bars[-1])], fut_vol=lambda b: 9000, pulse=pulse)
    assert s[0].checklist["oi"] == "CONFIRM"


def test_oi_confirms_rules() -> None:
    up = OIPulse(15, 100, 0, 0.9, 1.0, 5)
    assert oi_confirms(up, "bull") == "CONFIRM" and oi_confirms(up, "bear") == "REJECT"
    assert oi_confirms(OIPulse(15, 100, 0, 1.0, 0.9, 5), "bull") == "REJECT"  # PCR fell
    assert oi_confirms(OIPulse(15, -50, 80, 1.0, 0.9, 5), "bear") == "CONFIRM"
    assert oi_confirms(None, "bull") == "UNKNOWN"


def _pt(t: dt.datetime, spot: float, put: int, call: int) -> ChainPoint:
    strikes = [23800.0 + 50 * i for i in range(9)]
    return ChainPoint(t, spot, dict.fromkeys(strikes, put), dict.fromkeys(strikes, call))


def test_oi_pulse_tracker_window_and_staleness() -> None:
    d = dt.date(2026, 11, 30)
    tr = OIPulseTracker(window_min=15, k=3, max_age_s=90)
    for i in range(0, 20, 1):
        tr.add(_pt(ist(d, 10, i), 24000, 1000 + 10 * i, 1000))
    p = tr.pulse_at(ist(d, 10, 19) + dt.timedelta(seconds=30))
    # window starts 10:04:30 -> first snapshot 10:05; 14 minutes x 10 OI x 7 strikes
    assert p is not None and p.put_oi_change == 14 * 10 * 7 and p.call_oi_change == 0
    assert p.pcr_end > p.pcr_start
    assert tr.pulse_at(ist(d, 10, 25)) is None  # newest snapshot is 6 minutes old: stale
    assert OIPulseTracker(15, 3, 90).pulse_at(ist(d, 10, 0)) is None  # nothing recorded


def test_aggregator_emits_only_completed_15m() -> None:
    d = dt.date(2026, 11, 30)
    agg = Aggregator(15)
    out = [agg.add(bar(ist(d, 9, 15) + i * M, 1, 2, 0.5, 1.5, 1)) for i in range(16)]
    assert [o.ts for o in out if o] == [ist(d, 9, 15)] and out[14] is not None
    assert out[14].volume == 15


def test_trailing_exit() -> None:
    d = dt.date(2026, 11, 30)
    pos = OpenPosition("LONG_CE", 100, 90, None, 10, ist(d, 10, 0))
    v = lambda c: MarketView("NIFTY", ist(d, 10, 5), bar(ist(d, 10, 4), c, c, c, c))  # noqa: E731
    assert trail_exit(pos, v(105), "trail").action == "HOLD"
    t = trail_exit(pos, v(115), "trail")
    assert t.action == "TRAIL" and t.new_stop_underlying == Decimal("105")
    assert trail_exit(pos, v(115), "rr").action == "HOLD"


# ---------------------------------------------------------------- PDH / PDL
PD = Bar(ist(dt.date(2026, 11, 27), 9, 15), 24000, 24100, 23900, 24050)
D2 = dt.date(2026, 11, 30)


def pdh_run(p: PdhPdlBreakout, closes: list[float], day: dt.date = D2, take: bool = True):  # type: ignore[no-untyped-def]
    sigs = []
    t0 = ist(day, 9, 31)
    for i, c in enumerate(closes):
        b = Bar(t0 + i * M, c - 1, c + 1, c - 2, c)
        new = p.on_candle("1m", MarketView("NIFTY", b.ts + M, b, prior_day=PD))
        for s in new:
            if take:
                p.on_trade_opened(s)
        sigs += new
    return sigs


def warm(p: PdhPdlBreakout, day: dt.date = D2) -> None:
    pdh_run(p, [24050.0] * 45, day)  # ATR(14) warm-up inside the range


def test_pdh_breakout_fires_once_per_direction() -> None:
    p = PdhPdlBreakout(PdhPdlParams(), "b")
    warm(p)
    sigs = pdh_run(p, [24090, 24110, 24095, 24120, 23880, 23860])
    assert [s.direction for s in sigs] == ["LONG_CE", "LONG_PE"]  # second CE cross ignored
    ce = sigs[0]
    assert ce.checklist["pdh"] == 24100 and float(ce.reference_price) == 24110
    atr = float(ce.checklist["atr14"])  # type: ignore[arg-type]
    candle_stop, atr_stop = 24110 - 2 - 5, 24110 - 1.5 * atr
    assert float(ce.stop_underlying) == pytest.approx(max(candle_stop, atr_stop), abs=0.01)


def test_pdh_untaken_signal_can_fire_again_and_new_day_resets() -> None:
    p = PdhPdlBreakout(PdhPdlParams(), "b")
    warm(p)
    assert len(pdh_run(p, [24090, 24110, 24090, 24110], take=False)) == 2
    nxt = dt.date(2026, 12, 1)
    pdh_run(p, [24050.0] * 2, nxt)
    assert len(pdh_run(p, [24090, 24110], nxt)) == 1


def test_pdh_needs_a_cross_not_just_being_above() -> None:
    p = PdhPdlBreakout(PdhPdlParams(), "b")
    pdh_run(p, [24200.0] * 50)  # opened above PDH and stayed there
    assert pdh_run(p, [24210, 24220]) == []


def test_pdh_trend_filter_blocks_against_trend() -> None:
    p = PdhPdlBreakout(PdhPdlParams(trend_filter_enabled=True), "b")
    warm(p)
    assert pdh_run(p, [24090, 24110]) == []  # 15m EMA200 not warmed up -> no signal


def test_pdh_levels_skip_holidays_and_long_weekends() -> None:
    from app.research.dataset import UnderlyingData

    # Thu 2026-03-26, Fri 27 holiday, weekend, Mon 30 holiday, Tue 31 open (long weekend);
    # plus a special Saturday session on 2026-04-04 (e.g. a Budget day).
    days = [
        dt.date(2026, 3, 26),
        dt.date(2026, 3, 31),
        dt.date(2026, 4, 1),
        dt.date(2026, 4, 4),
        dt.date(2026, 4, 6),
    ]
    daily = {d: Bar(ist(d, 9, 15), 100 + i, 110 + i, 90 + i, 105 + i) for i, d in enumerate(days)}
    u = UnderlyingData("NIFTY", [], {}, daily, days)
    assert u.prior_day(dt.date(2026, 3, 31)) == daily[dt.date(2026, 3, 26)]  # after long weekend
    assert u.prior_day(dt.date(2026, 4, 6)) == daily[dt.date(2026, 4, 4)]  # special session
    assert u.prior_day(dt.date(2026, 4, 4)) == daily[dt.date(2026, 4, 1)]
    assert u.prior_day(dt.date(2026, 3, 26)) is None  # no earlier data: no levels, no signal
