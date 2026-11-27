"""Fill rules and review fixes, pinned with a stub plugin that signals at a chosen candle."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine

from app.config.loader import REPO_ROOT, load_config
from app.config.schema import PdhPdlParams, PlatformConfig
from app.core.types import Segment
from app.data.bars import Bar
from app.research.backtest import BookSpec, Pricer, build_views, roll_reset_days, simulate
from app.research.contracts import ExpiryCalendar, load_specs
from app.research.costs import ChargesTable
from app.research.dataset import Dataset, UnderlyingData, load_chain_points
from app.strategies.base import (
    DataNeeds,
    ExitDecision,
    MarketView,
    OpenPosition,
    Signal,
    StrategyPlugin,
    WarmupRequirement,
)
from app.strategies.ema200_hammer import trail_exit
from tests.helpers import ist

M = dt.timedelta(minutes=1)
WED = dt.date(2026, 11, 25)
TUE_EXPIRY = dt.date(2026, 12, 1)
SPECS = load_specs(REPO_ROOT / "config" / "contracts.yaml")


class Stub(StrategyPlugin):
    id: ClassVar[str] = "stub"
    segment: ClassVar[Segment] = Segment.OPTIONS
    params_model: ClassVar[type[BaseModel]] = PdhPdlParams
    plan: ClassVar[dict[dt.datetime, tuple[str, float, float]]] = {}
    trail: ClassVar[bool] = False

    @classmethod
    def data_needs(cls, params: BaseModel) -> DataNeeds:
        return DataNeeds(timeframes=("1m",))

    @classmethod
    def warmup(cls, params: BaseModel) -> WarmupRequirement:
        return WarmupRequirement(bars={})

    def on_candle(self, timeframe: str, view: MarketView) -> list[Signal]:
        p = self.plan.get(view.bar.ts)
        if p is None:
            return []
        direction, stop, target = p
        return [
            Signal(
                strategy_id="stub",
                underlying=view.underlying,
                direction=direction,
                at=view.now,
                reference_price=Decimal(str(view.bar.close)),
                stop_underlying=Decimal(str(stop)),
                target_underlying=Decimal(str(target)),
                checklist={},
                config_hash="stub",
            )
        ]

    def exit_logic(self, position: OpenPosition, view: MarketView) -> ExitDecision:
        return trail_exit(position, view, "trail" if self.trail else "rr")


def day_bars(d: dt.date, custom: dict[int, tuple[float, float, float, float]]) -> list[Bar]:
    """Flat 100.0 candles all day, except the given minute offsets from 09:15."""
    out = []
    for i in range(375):
        o, h, lo, c = custom.get(i, (100.0, 100.5, 99.5, 100.0))
        out.append(Bar(ist(d, 9, 15) + i * M, o, h, lo, c, 1000))
    return out


def run(
    bars: list[Bar],
    plan: dict[dt.datetime, tuple[str, float, float]],
    *,
    cfg: PlatformConfig | None = None,
    trail: bool = False,
    option_bars: dict[str, dict[dt.datetime, Bar]] | None = None,
    day: dt.date = WED,
) -> tuple[Any, Any]:
    cfg = cfg or load_config()
    Stub.plan, Stub.trail = plan, trail
    prev = day - dt.timedelta(days=1)
    u = UnderlyingData(
        "NIFTY",
        bars,
        {b.ts: b for b in bars},
        {prev: Bar(ist(prev, 9, 15), 100, 101, 99, 100)},
        [prev, day],
    )
    ds = Dataset(
        {"NIFTY": u}, [], {prev: Bar(ist(prev, 9, 15), 13, 13, 13, 13)}, option_bars or {}, {}
    )
    cals = {"NIFTY": ExpiryCalendar(SPECS["NIFTY"], u.trading_days)}
    pricer = Pricer(cfg, ds, SPECS, cals, ChargesTable(REPO_ROOT / "config" / "charges.yaml"))
    views = {"NIFTY": build_views(u, cals["NIFTY"], 15, 3, 90)}
    res = simulate(
        cfg,
        ds,
        [BookSpec("T", "stub", lambda: Stub(PdhPdlParams(), "stub"))],
        views,
        pricer,
        day,
        day,
    )
    return res[0], pricer


def at(d: dt.date, minute: int) -> dt.datetime:
    return ist(d, 9, 15) + minute * M


# signal on the 10:00 candle (offset 45); entry at the 10:01 open (offset 46)
SIG, ENTRY = 45, 46


def test_stop_beats_target_in_the_same_candle() -> None:
    bars = day_bars(WED, {ENTRY: (100.0, 125.0, 85.0, 100.0)})
    r, _ = run(bars, {at(WED, SIG): ("LONG_CE", 90.0, 120.0)})
    t = r.trades[0]
    assert (t.exit_reason, t.exit, round(t.r_multiple, 2)) == ("STOP", 90.0, -1.0)


def test_gap_through_stop_exits_at_open() -> None:
    bars = day_bars(WED, {ENTRY + 2: (80.0, 81.0, 79.0, 80.0)})
    r, _ = run(bars, {at(WED, SIG): ("LONG_CE", 90.0, 120.0)})
    t = r.trades[0]
    assert (t.exit_reason, t.exit) == ("STOP_GAP", 80.0) and t.r_multiple == -2.0


def test_square_off_at_1510_open() -> None:
    r, _ = run(day_bars(WED, {}), {at(WED, SIG): ("LONG_CE", 90.0, 120.0)})
    t = r.trades[0]
    assert t.exit_reason == "SQUARE_OFF" and t.exit_at == ist(WED, 15, 10)


def test_long_pe_is_short_the_index() -> None:
    bars = day_bars(WED, {ENTRY + 3: (100.0, 100.5, 79.0, 80.0)})
    r, _ = run(bars, {at(WED, SIG): ("LONG_PE", 110.0, 80.0)})
    t = r.trades[0]
    assert (t.exit_reason, t.exit, t.points, t.r_multiple) == ("TARGET", 80.0, 20.0, 2.0)
    assert t.modeled[1.0].option_type == "PE" and t.modeled[1.0].net_1lot > 0


def test_entry_gapping_through_stop_is_skipped() -> None:
    bars = day_bars(WED, {ENTRY: (88.0, 89.0, 87.0, 88.0)})
    r, _ = run(bars, {at(WED, SIG): ("LONG_CE", 90.0, 120.0)})
    assert r.trades == [] and r.rejected["GAPPED_THROUGH_STOP"] == 1


def test_target_is_two_r_from_the_actual_entry() -> None:
    bars = day_bars(WED, {ENTRY: (102.0, 102.5, 101.5, 102.0)})  # opens 2 above signal close
    r, _ = run(bars, {at(WED, SIG): ("LONG_CE", 90.0, 120.0)})  # 2R from close 100
    assert r.trades[0].target == pytest.approx(102.0 + 2 * 12.0)


def test_trailing_stop_moves_and_exits() -> None:
    custom = {
        ENTRY + i: (100.0 + 2 * i, 102.5 + 2 * i, 99.5 + 2 * i, 102.0 + 2 * i) for i in range(10)
    }  # rallies 20 points
    custom[ENTRY + 10] = (120.0, 120.5, 105.0, 106.0)  # then drops through the trail
    r, _ = run(day_bars(WED, custom), {at(WED, SIG): ("LONG_CE", 90.0, 999.0)}, trail=True)
    t = r.trades[0]
    assert t.exit_reason == "STOP" and t.exit == pytest.approx(110.0)  # trailed 1R behind


def test_expiry_day_skip_policy() -> None:
    base = load_config().model_dump(mode="json")
    base["contracts"]["nifty_expiry_day"] = "skip"
    cfg = PlatformConfig.model_validate(base)
    r, _ = run(
        day_bars(TUE_EXPIRY, {}),
        {at(TUE_EXPIRY, SIG): ("LONG_CE", 90.0, 120.0)},
        cfg=cfg,
        day=TUE_EXPIRY,
    )
    assert r.trades == [] and r.rejected["EXPIRY_DAY_SKIP"] == 1
    r2, _ = run(
        day_bars(TUE_EXPIRY, {}), {at(TUE_EXPIRY, SIG): ("LONG_CE", 90.0, 120.0)}, day=TUE_EXPIRY
    )  # default next_weekly: trades the following week's contract
    assert r2.trades[0].modeled[1.0].expiry == dt.date(2026, 12, 8)


def test_layer3_uses_recorded_option_candles() -> None:
    bars = day_bars(WED, {ENTRY + 3: (100.0, 121.0, 99.5, 120.0)})
    key = "OPT|NIFTY|2026-12-01|100|CE"
    opt = {
        at(WED, ENTRY): Bar(at(WED, ENTRY), 50, 51, 49, 50),
        at(WED, ENTRY + 4): Bar(at(WED, ENTRY + 4), 60, 61, 59, 60),
    }
    r, _ = run(bars, {at(WED, SIG): ("LONG_CE", 90.0, 120.0)}, option_bars={key: opt})
    t = r.trades[0]
    assert t.real is not None and t.real.entry_premium > 50 and t.real.exit_premium < 60
    assert t.real.net_1lot == pytest.approx(
        (t.real.exit_premium - t.real.entry_premium) * 65 - t.real.charges_1lot
    )


def test_backstop_check_bites_when_its_multiple_is_raised() -> None:
    base = load_config().model_dump(mode="json")
    base["contracts"]["backstop_delta_multiple"] = 3.0
    _, pricer = run(day_bars(WED, {}), {}, cfg=PlatformConfig.model_validate(base))
    # premium stop of 10 points x 65 = Rs 650 fits the Rs 1,000 budget for 1 lot, but the
    # backstop at 3 x 10 x 65 = Rs 1,950 exceeds 1.5 x 1,000: skip.
    leg = pricer._price(
        WED, 24000, TUE_EXPIRY, "CE", 65, 120.0, 130.0, 0.13, stop_dist=10.0, risk_inr=1000.0
    )
    assert leg.skip_reason == "SKIPPED_BACKSTOP" and leg.lots_sized == 0
    _, default = run(day_bars(WED, {}), {})
    ok = default._price(
        WED, 24000, TUE_EXPIRY, "CE", 65, 120.0, 130.0, 0.13, stop_dist=10.0, risk_inr=1000.0
    )
    assert ok.skip_reason is None and ok.lots_sized == 1  # default multiples: implied, passes


def test_expiry_calendar_before_the_data_starts() -> None:
    cal = ExpiryCalendar(SPECS["NIFTY"], [dt.date(2026, 11, 30), dt.date(2026, 12, 1)])
    assert cal.monthly(2026, 11) == dt.date(2026, 11, 24)  # used to loop forever


def test_vix_uses_only_closed_candles() -> None:
    t0 = dt.datetime(2026, 11, 30, 4, 29, tzinfo=dt.UTC)
    ds = Dataset({}, [Bar(t0, 14, 14, 14, 14), Bar(t0 + M, 20, 20, 20, 20)], {}, {}, {})
    assert ds.vix_at(t0 + M) == 14  # the 04:30 candle hasn't closed yet
    assert ds.vix_at(t0 + 2 * M) == 20


def test_roll_reset_days() -> None:
    days = [
        d for d in (dt.date(2026, 12, 1) + dt.timedelta(i) for i in range(40)) if d.weekday() < 5
    ]
    cal = ExpiryCalendar(SPECS["NIFTY"], days)
    exp = cal.monthly(2026, 12)
    i = days.index(exp)
    assert roll_reset_days(days, cal, 3) == {days[i - 2], days[i + 1]}


def test_chain_points_keep_only_the_traded_expiry_on_expiry_day(migrated_engine: Engine) -> None:
    from app.adapters.base import ChainRow, ChainSnapshot
    from app.core.types import InstrumentKey, OptionType
    from app.services.recorder import store_snapshot

    def snap(exp: dt.date, oi: int, ts: dt.datetime) -> ChainSnapshot:
        rows = tuple(
            ChainRow(
                strike=Decimal(k),
                option_type=OptionType(ot),
                key=InstrumentKey.option("NIFTY", exp, k, OptionType(ot)),
                bid=None,
                ask=None,
                ltp=None,
                volume=None,
                oi=oi,
            )
            for k in range(23850, 24200, 50)
            for ot in ("CE", "PE")
        )
        return ChainSnapshot(
            underlying="NIFTY",
            expiry=exp,
            taken_at=ts,
            spot=Decimal("24000"),
            india_vix=None,
            rows=rows,
        )

    for m in range(3):
        ts = ist(TUE_EXPIRY, 10, m)
        store_snapshot(migrated_engine, snap(TUE_EXPIRY, 55_000_000, ts))
        store_snapshot(
            migrated_engine, snap(dt.date(2026, 12, 8), 11_000_000, ts + dt.timedelta(seconds=1))
        )
    pts = load_chain_points(migrated_engine, "NIFTY", TUE_EXPIRY, TUE_EXPIRY)
    assert len(pts) == 3 and {p.put_oi[24000.0] for p in pts} == {11_000_000}
    skip = load_chain_points(migrated_engine, "NIFTY", TUE_EXPIRY, TUE_EXPIRY, "skip")
    assert {p.put_oi[24000.0] for p in skip} == {55_000_000}


def test_weekly_loss_breaker(tmp_path: Path) -> None:
    from app.research.backtest import Leg, Trade, apply_breakers

    cfg = load_config()  # weekly limit 8% of 50,000 = 4,000; daily 1,500
    trades = []
    for i, d in enumerate(
        [dt.date(2026, 11, 23), dt.date(2026, 11, 24), dt.date(2026, 11, 25), dt.date(2026, 11, 26)]
    ):
        a = ist(d, 10, 0)
        t = Trade("T", "x", "NIFTY", "LONG_CE", a, a, 100, 90, 120, 10, {}, "h")
        t.modeled[1.0] = Leg(100, d, "CE", 65, 1, 1, 0.1, 1, None, -1400, 1, -1400 + i * 0, 1)
        trades.append(t)
    kept, why = apply_breakers(trades, cfg)
    assert kept == [-1400, -1400, -1400] and why["WEEKLY_LOSS_BREAKER"] == 1
