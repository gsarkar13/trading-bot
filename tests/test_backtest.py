import datetime as dt
import json
from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.config.loader import load_config
from app.config.schema import PlatformConfig
from app.data.bars import Bar
from app.data.store import upsert_bars
from app.research.backtest import Trade, apply_breakers
from app.tools import backtest as bt
from tests.helpers import ist, trading_days

M = dt.timedelta(minutes=1)
DAYS = trading_days(dt.date(2026, 9, 1), 32)
LAST = DAYS[-1]


def flat_day(d: dt.date, base: float, amp: float) -> list[Bar]:
    """A quiet day oscillating inside base +/- amp."""
    out = []
    for i in range(375):
        o = base + (amp if (i // 7) % 2 else -amp) * ((i % 7) / 7)
        c = o + (0.3 if i % 2 else -0.3)
        out.append(Bar(ist(d, 9, 15) + i * M, o, max(o, c) + 0.4, min(o, c) - 0.4, c, 1000))
    return out


def breakout_day(d: dt.date, base: float) -> list[Bar]:
    """Quiet until 10:00, then a clean break above the previous day's high and a rally."""
    bars = flat_day(d, base, 5)[:45]  # 09:15..09:59
    px = bars[-1].close
    for i in range(45, 375):
        step = 2.0 if i < 160 else 0.0
        o = px
        c = o + step
        bars.append(Bar(ist(d, 9, 15) + i * M, o, max(o, c) + 0.5, min(o, c) - 0.5, c, 1000))
        px = c
    return bars


def seed(engine: Engine) -> None:
    for u, base in (("NIFTY", 24000.0), ("BANKNIFTY", 55000.0)):
        idx, fut = [], []
        for d in DAYS[:-1]:
            idx += flat_day(d, base, 8)
        idx += breakout_day(LAST, base) if u == "NIFTY" else flat_day(LAST, base, 8)
        fut = [Bar(b.ts, b.open, b.high, b.low, b.close, 2000) for b in idx]
        upsert_bars(engine, f"INDEX|{u}", "1m", "fyers_hist", idx)
        upsert_bars(engine, f"FUTC|{u}", "1m", "fyers_hist", fut)
    vix = [Bar(ist(d, 9, 15), 13, 13, 13, 13) for d in DAYS]
    upsert_bars(engine, "INDEX|INDIAVIX", "1d", "fyers_hist", vix)


@pytest.fixture
def cfg(tmp_path: Path) -> PlatformConfig:
    base = load_config().model_dump(mode="json")
    base["research"]["out_of_sample_from"] = LAST.isoformat()
    base["research"]["frozen_registry"] = str(tmp_path / "frozen.json")
    base["research"]["bootstrap_samples"] = 200
    return PlatformConfig.model_validate(base)


def test_backtest_end_to_end_pdh_trade(
    migrated_engine: Engine, cfg: PlatformConfig, tmp_path: Path
) -> None:
    seed(migrated_engine)
    s = bt.run(migrated_engine, cfg, DAYS[0], LAST, "all", False, tmp_path)
    html_file = Path(s["_files"][1])
    assert html_file.exists() and "MODELED" in html_file.read_text()
    assert set(s["books"]) == {"S1_A", "S1_B", "S2_P", "S2_PT"}
    p = s["books"]["S2_P"]
    assert p["trades"] == 1, p
    trades = json.loads(Path(s["_files"][0]).read_text())["books"]["S2_P"]
    assert trades["layer1_underlying"]["exit_reasons"] == {"TARGET": 1}
    assert trades["layer1_underlying"]["first_touch_2R_before_stop"] == 1.0
    l2 = trades["layer2_MODELED_option"]["iv_x1.0"]
    one = l2["one_lot_net_inr"]
    assert one["n"] == 1 and one["expectancy"] > 0
    assert l2["avg_charges_1lot_inr"] > 0
    assert set(trades["layer2_MODELED_option"]) == {"iv_x0.8", "iv_x1.0", "iv_x1.2"}
    assert trades["layer3_REAL_option"]["trades_with_real_option_data"] == 0
    assert s["books"]["S1_A"]["vs_baseline"]["verdict"]  # compared with the baseline
    assert s["meta"]["data_coverage"]["NIFTY"]["futures_bar_coverage"] == 1.0
    assert s["meta"]["pricing_notes"]["lot_size_unverified"] >= 1  # flagged, not hidden


def test_out_of_sample_requires_frozen_hash(
    migrated_engine: Engine, cfg: PlatformConfig, tmp_path: Path
) -> None:
    seed(migrated_engine)
    with pytest.raises(bt.OOSRefused, match="run `freeze` first"):
        bt.run(migrated_engine, cfg, DAYS[0], LAST, "out-of-sample", False, tmp_path)
    with pytest.raises(bt.OOSRefused, match="in-sample data only"):
        bt.run(migrated_engine, cfg, DAYS[0], LAST, "all", True, tmp_path)
    bt.freeze(cfg, Path(cfg.research.frozen_registry), dt.datetime.now(dt.UTC))
    s = bt.run(migrated_engine, cfg, DAYS[0], LAST, "out-of-sample", False, tmp_path)
    assert s["meta"]["from"] == LAST.isoformat()
    changed = cfg.model_dump(mode="json")
    changed["strategies"][1]["params"] = {"buffer_value": 0.5}  # tuned after freezing
    with pytest.raises(bt.OOSRefused, match="never frozen"):
        bt.run(
            migrated_engine,
            PlatformConfig.model_validate(changed),
            DAYS[0],
            LAST,
            "out-of-sample",
            False,
            tmp_path,
        )


def test_grid_runs_in_sample(migrated_engine: Engine, cfg: PlatformConfig, tmp_path: Path) -> None:
    seed(migrated_engine)
    small = cfg.model_dump(mode="json")
    small["research"].update(
        {
            "grid_rvol": [2.0],
            "grid_tol": [0.25],
            "grid_min_range_atr": [0.8],
            "grid_pdh_buffer": [0.1, 0.25],
        }
    )
    s = bt.run(
        migrated_engine,
        PlatformConfig.model_validate(small),
        DAYS[0],
        LAST,
        "in-sample",
        True,
        tmp_path,
    )
    assert len(s["grid"]["S2_P"]["cells"]) == 2 and "stability" in s["grid"]["S2_P"]


def _trade(day: int, net: float, cfg: PlatformConfig) -> Trade:
    from app.research.backtest import Leg

    at = ist(DAYS[day], 10, 0)
    t = Trade("S2_P", "x", "NIFTY", "LONG_CE", at, at, 100, 90, 120, 10, {}, "h")
    t.modeled[1.0] = Leg(100, DAYS[day], "CE", 65, 1, 1, 0.1, 1, None, net, 1, net, 1)
    return t


def test_breakers_daily_loss_and_streak(cfg: PlatformConfig) -> None:
    # daily limit 3% of 50,000 = 1,500
    day0 = [_trade(0, -900, cfg), _trade(0, -900, cfg), _trade(0, 500, cfg)]
    kept, why = apply_breakers(day0, cfg)
    assert kept == [-900, -900] and why["DAILY_LOSS_BREAKER"] == 1
    streak = [
        _trade(1, -10, cfg),
        _trade(1, -10, cfg),
        _trade(2, -10, cfg),
        _trade(2, 50, cfg),
        _trade(3, 50, cfg),
    ]
    kept, why = apply_breakers(streak, cfg)
    assert kept == [-10, -10, -10, 50] and why["LOSS_STREAK_BREAKER"] == 1  # reset next day


def test_strategy1_trade_end_to_end(
    migrated_engine: Engine, cfg: PlatformConfig, tmp_path: Path
) -> None:
    """A hammer planted at the 1m EMA200 in an uptrend, with a futures-volume spike, must
    become exactly one S1_A trade (S1_B rejects it: no chain snapshots, OI unknown)."""
    from app.config.schema import Ema200HammerParams
    from app.strategies.base import MarketView
    from app.strategies.ema200_hammer import Ema200HammerRvolOi
    from tests.test_strategies import trend_sessions

    days = trading_days(dt.date(2026, 6, 1), 27)
    bars = trend_sessions(days[:-1], drift=0.1)
    probe = Ema200HammerRvolOi(Ema200HammerParams(), "p")
    for b in bars:
        probe.on_candle("1m", MarketView("NIFTY", b.ts + M, b, futures_bar=b))
    st = probe._s["NIFTY"]
    # last day: quiet trend until 10:30, then the hammer, then a rally
    last = days[-1]
    px = bars[-1].close
    day = []
    for i in range(375):
        ts = ist(last, 9, 15) + i * M
        if i == 75:  # 10:30 hammer at the EMA (values tracked on the previous day's close)
            ema, atr = st.ema.value or px, st.atr.value or 1.0
            c = max(px, ema + 0.5)
            day.append(Bar(ts, c - 0.2, c + 0.1, ema - 3 * atr, c, 9000))
            px = c
            continue
        step = 0.5 if i > 75 else 0.1 * (1 if i % 2 else -1)
        o, c = px, px + step
        day.append(Bar(ts, o, max(o, c) + 0.3, min(o, c) - 0.3, c, 1000))
        px = c
    allbars = bars + day
    upsert_bars(migrated_engine, "INDEX|NIFTY", "1m", "fyers_hist", allbars)
    upsert_bars(migrated_engine, "FUTC|NIFTY", "1m", "fyers_hist", allbars)  # volume = bar's
    upsert_bars(
        migrated_engine,
        "INDEX|INDIAVIX",
        "1d",
        "fyers_hist",
        [Bar(ist(d, 9, 15), 13, 13, 13, 13) for d in days],
    )
    s = bt.run(migrated_engine, cfg, last, last, "all", False, tmp_path)
    a, b = s["books"]["S1_A"], s["books"]["S1_B"]
    assert a["signals"] >= 1 and a["trades"] == 1, a
    assert b["trades"] == 0 and b["rejected"].get("FILTERED_BY_BOOK", 0) >= 1
