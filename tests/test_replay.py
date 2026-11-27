import datetime as dt
from decimal import Decimal

from sqlalchemy import Engine

from app.adapters.base import TickData
from app.core.types import InstrumentKey
from app.data.store import upsert_bars
from app.services.recorder import store_ticks
from app.tools.replay import compare, load_ticks, rebuild
from tests.helpers import ist

D = dt.date(2026, 11, 30)
K = InstrumentKey.index("NIFTY")


def test_replay_rebuilds_candles_identical_to_live(migrated_engine: Engine) -> None:
    ticks = []
    for m in range(5):
        for s, px in ((1, 100.0 + m), (20, 103.0 + m), (40, 99.0 + m), (59, 101.0 + m)):
            ts = ist(D, 9, 15 + m) + dt.timedelta(seconds=s)
            ticks.append(TickData(key=K, exchange_ts=ts, received_ts=ts, ltp=Decimal(str(px))))
    ticks.insert(3, ticks[2])  # a duplicate, as the socket sometimes sends
    store_ticks(migrated_engine, ticks)
    live, _ = rebuild(ticks)  # what the recorder built live
    upsert_bars(migrated_engine, K.canonical, "1m", "fyers_hist", live)
    loaded = load_ticks(migrated_engine, K.canonical, D)
    replayed, stats = rebuild(loaded)
    assert replayed == live and stats["duplicates"] == 1
    cmp = compare(replayed, live)
    assert cmp["ohlc_mismatches"] == 0 and cmp["matched_minutes"] == 5


def test_replay_dataset_swaps_in_rebuilt_candles() -> None:
    from app.data.bars import Bar
    from app.research.dataset import Dataset, UnderlyingData
    from app.tools.replay import replay_dataset

    prev = D - dt.timedelta(days=3)
    old = [Bar(ist(prev, 9, 15), 1, 1, 1, 1), Bar(ist(D, 9, 15), 5, 5, 5, 5)]
    u = UnderlyingData("NIFTY", old, {}, {}, [prev])
    ds = Dataset({"NIFTY": u}, [], {}, {}, {})
    new = [Bar(ist(D, 9, 15), 7, 7, 7, 7)]
    out = replay_dataset(ds, D, {"NIFTY": new}, {"NIFTY": new})
    nifty = out.per_underlying["NIFTY"]
    assert [b.close for b in nifty.bars] == [1, 7] and nifty.trading_days == [prev, D]
    assert nifty.futures[new[0].ts].close == 7
