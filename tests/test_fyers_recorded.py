"""Contract tests on REAL Fyers responses, recorded by the trader's probe on 2026-11-28
(tests/fixtures/fyers/recorded/; public market data, checked for secrets)."""

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.adapters.fyers.chain import parse_chain
from app.adapters.fyers.client import check, parse_candles
from app.adapters.fyers.symbols import SymbolMap, parse_master
from app.adapters.fyers.ws import parse_tick
from app.core.timeutil import IST, to_ist
from app.core.types import InstrumentKey, OptionType
from app.services.recorder import _expiry_epochs

FIX = Path(__file__).parent / "fixtures" / "fyers"
REC = FIX / "recorded"
NOW = dt.datetime(2026, 11, 28, 12, 0, tzinfo=dt.UTC)


def rec(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((REC / f"{name}.json").read_text())
    return data


@pytest.fixture(scope="module")
def smap() -> SymbolMap:
    text = "".join(
        (FIX / f).read_text() for f in ("NSE_FO_probe_2026-11-28.csv", "NSE_CM_sample.csv")
    )
    return SymbolMap(parse_master(text))


def test_real_nifty_chain_parses(smap: SymbolMap) -> None:
    resp = check(rec("optionchain_nifty"), "chain")
    snap = parse_chain(resp, "NIFTY", smap, NOW)
    assert snap.spot == Decimal("25607.2") and snap.india_vix == Decimal("16.55")
    assert snap.expiry == dt.date(2026, 12, 1) and len(snap.rows) == 10  # 5 strikes x CE/PE
    pe = next(r for r in snap.rows if r.strike == 25500 and r.option_type is OptionType.PE)
    assert pe.key == InstrumentKey.option("NIFTY", dt.date(2026, 12, 1), 25500, OptionType.PE)
    assert (pe.bid, pe.ask, pe.oi, pe.oi_change) == (
        Decimal("109.45"),
        Decimal("110.5"),
        7435155,
        -139485,
    )
    assert dict(_expiry_epochs(resp))[dt.date(2026, 12, 1)] == 1796119800


def test_real_banknifty_chain_with_greeks(smap: SymbolMap) -> None:
    snap = parse_chain(rec("optionchain_banknifty_greeks"), "BANKNIFTY", smap, NOW)
    assert snap.expiry == dt.date(2026, 12, 29) and len(snap.rows) == 10
    ce = next(r for r in snap.rows if r.strike == 62800 and r.option_type is OptionType.CE)
    assert ce.iv == Decimal("17.28")  # Fyers gives IV in PERCENT (17.28 = 17.28%)
    assert ce.delta == Decimal("0.57")


def test_real_history_candles() -> None:
    idx = parse_candles(check(rec("history_index_1m"), "h"))
    assert to_ist(idx[0].ts) == dt.datetime(2026, 11, 23, 9, 15, tzinfo=IST)  # candle OPEN
    assert idx[0].volume > 0  # Fyers now reports index volume (we still use futures)
    fut = parse_candles(check(rec("history_future_cont_1m"), "h"))
    assert fut[0].oi == 6699940  # 7th column = OI with oi_flag=1
    vix = parse_candles(check(rec("history_vix_daily"), "h"))
    assert to_ist(vix[0].ts).date() == dt.date(2026, 10, 29)  # daily ts = 00:00 UTC


def test_real_socket_messages(smap: SymbolMap) -> None:
    msgs = rec("socket_messages")
    ticks = [t for m in msgs if (t := parse_tick(m, smap, NOW)) is not None]
    assert {t.key for t in ticks} == {
        InstrumentKey.index("NIFTY"),
        InstrumentKey.index("INDIAVIX"),
        InstrumentKey.future("NIFTY", dt.date(2026, 12, 29)),
    }
    fut = next(t for t in ticks if t.key.kind == "FUT")
    assert fut.cum_volume == 3216915 and fut.bid == Decimal("25698.3")
    # last quote of the day arrives AFTER the 15:30 close (15:40 futures, 15:59 index)
    assert to_ist(fut.exchange_ts) == dt.datetime(2026, 11, 27, 15, 40, tzinfo=IST)
    # control messages ("cn", "ful", "sub", close) are not ticks
    assert sum(1 for m in msgs if m.get("type") in ("cn", "ful", "sub")) == 3
