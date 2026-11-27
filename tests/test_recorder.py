import asyncio
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.adapters.base import TickData
from app.adapters.fyers.symbols import SymbolMap, parse_master
from app.core.types import InstrumentKey
from app.data.bars import Bar
from app.db.models import Candle, OptionChainSnapshot, Tick
from app.services.recorder import Recorder, is_trading_day, trading_expiries

FIX = Path(__file__).parent / "fixtures" / "fyers"
DAY = dt.date(2026, 11, 30)


def smap() -> SymbolMap:
    return SymbolMap(
        parse_master(
            (FIX / "NSE_FO_sample.csv").read_text() + (FIX / "NSE_CM_sample.csv").read_text()
        )
    )


class FakeClient:
    def __init__(self) -> None:
        self.history_calls: list[tuple[str, str, bool]] = []

    def option_chain(
        self, symbol: str, strikecount: int, expiry_epoch: int | None = None
    ) -> dict[str, Any]:
        u, strike, step, exp = (
            ("NIFTY", 24000, 50, "26D01")
            if "NIFTY50" in symbol
            else ("BANKNIFTY", 55000, 100, "26DEC")
        )
        rows = [{"symbol": "x", "strike_price": -1, "ltp": strike + 3}]
        for k in range(-1, 2):
            for ot in ("CE", "PE"):
                rows.append(
                    {
                        "symbol": f"NSE:{u}{exp}{strike + k * step}{ot}",
                        "strike_price": strike + k * step,
                        "option_type": ot,
                        "bid": 100,
                        "ask": 101,
                        "ltp": 100.5,
                        "oi": 1000,
                        "volume": 10,
                    }
                )
        return {
            "s": "ok",
            "data": {"optionsChain": rows, "indiavixData": {"ltp": 12.5}, "expiryData": []},
        }

    def history(
        self,
        symbol: str,
        resolution: str,
        start: dt.date,
        end: dt.date,
        cont_flag: bool = False,
        oi_flag: bool = False,
    ) -> list[Bar]:
        self.history_calls.append((symbol, resolution, cont_flag))
        t = dt.datetime(2026, 11, 30, 3, 45, tzinfo=dt.UTC)
        return [Bar(t, 1, 2, 0.5, 1.5, 10)]


class FakeStream:
    def __init__(self, on_tick: Any, on_status: Any, sm: SymbolMap) -> None:
        self.on_tick, self.on_status, self.sm = on_tick, on_status, sm
        self.subscribed: list[str] = []
        self.stopped = False

    def start(self) -> None:
        self.on_status("connect", {})

    def subscribe(self, symbols: list[str]) -> None:
        self.subscribed += symbols
        now = dt.datetime.now(dt.UTC)
        for i, sym in enumerate(symbols):
            for j in range(3):
                self.on_tick(
                    TickData(
                        key=self.sm.key_for(sym),
                        exchange_ts=now,
                        received_ts=now,
                        ltp=Decimal(100 + i + j),
                        cum_volume=10 * j,
                    )
                )

    def stop(self) -> None:
        self.stopped = True


def test_trading_expiries_add_next_weekly_on_expiry_day() -> None:
    sm = smap()
    assert trading_expiries(sm, "NIFTY", dt.date(2026, 11, 30)) == [dt.date(2026, 12, 1)]
    assert trading_expiries(sm, "NIFTY", dt.date(2026, 12, 1)) == [
        dt.date(2026, 12, 1),
        dt.date(2026, 12, 8),
    ]
    assert trading_expiries(sm, "BANKNIFTY", DAY) == [dt.date(2026, 12, 29)]


def test_trading_day_check(tmp_path: Path) -> None:
    assert is_trading_day(dt.date(2026, 11, 28), tmp_path)[0] is False  # Saturday
    ok, why = is_trading_day(DAY, tmp_path)
    assert ok and "no calendar file" in why
    (tmp_path / "nse_2026.yaml").write_text("holidays:\n  - {date: '2026-11-30', name: X}\n")
    assert is_trading_day(DAY, tmp_path) == (False, "NSE holiday (calendar file)")


def test_recorder_session_end_to_end(migrated_engine: Engine, tmp_path: Path) -> None:
    sm = smap()
    client = FakeClient()
    streams: list[FakeStream] = []

    def factory(on_tick: Any, on_status: Any) -> FakeStream:
        streams.append(FakeStream(on_tick, on_status, sm))
        return streams[0]

    now = dt.datetime.now(dt.UTC)
    db_path = Path(str(migrated_engine.url.database))
    rec = Recorder(
        migrated_engine,
        client,
        sm,
        factory,
        day=DAY,
        underlyings=("NIFTY", "BANKNIFTY"),
        chain_interval_s=0.5,
        n_strikes=1,
        start=now,
        stop=now + dt.timedelta(seconds=2.5),
        db_path=db_path,
        report_dir=tmp_path / "rep",
        backup_dir=tmp_path / "bak",
    )
    asyncio.run(rec.run_live())
    s = streams[0]
    assert s.stopped
    assert "NSE:NIFTY50-INDEX" in s.subscribed and "NSE:NIFTY26DECFUT" in s.subscribed
    assert "NSE:INDIAVIX-INDEX" in s.subscribed and len(s.subscribed) == 5
    with Session(migrated_engine) as db:
        assert db.scalar(select(func.count(Tick.id))) == 15
        assert db.scalar(select(func.count(Candle.id)).where(Candle.source == "fyers_ws")) == 5
        snaps = db.scalar(select(func.count(func.distinct(OptionChainSnapshot.snapshot_id))))
        assert snaps is not None and snaps >= 4  # >= 2 rounds x 2 underlyings
    summary = rec.end_of_day(0)
    assert summary["option_contracts_seen"] == 12  # 3 strikes x CE/PE x 2 underlyings
    assert summary["option_contracts_with_rest_candles"] == 12
    assert ("NSE:NIFTY26DECFUT", "1", True) in client.history_calls  # continuous future
    assert summary["rest_candles"]["FUTC|NIFTY 1m"] == 1
    assert Path(summary["backup"]).exists()
    saved = json.loads((tmp_path / "rep" / f"{DAY}.json").read_text())
    assert saved["ticks"]["INDEX|NIFTY"] == 3 and "projection_12_months_gb" in saved
    with Session(migrated_engine) as db:
        assert (
            db.scalar(
                select(func.count(Candle.id)).where(
                    Candle.instrument_key
                    == InstrumentKey.option("NIFTY", dt.date(2026, 12, 1), 24050, "CE").canonical
                )
            )
            == 1
        )  # type: ignore[arg-type]


def test_save_master_keeps_only_useful_rows(migrated_engine: Engine) -> None:
    from app.db.models import Instrument
    from app.services.recorder import save_master

    infos = parse_master(
        (FIX / "NSE_FO_sample.csv").read_text() + (FIX / "NSE_CM_sample.csv").read_text()
    )
    n = save_master(migrated_engine, infos, DAY, ("NIFTY",))
    with Session(migrated_engine) as db:
        kinds = {(k, u) for k, u in db.execute(select(Instrument.kind, Instrument.underlying))}
    assert ("OPT", "BANKNIFTY") not in kinds and ("OPT", "NIFTY") in kinds
    assert ("INDEX", "BANKNIFTY") in kinds and n < len(infos)
    assert save_master(migrated_engine, infos, DAY) == 0  # once per day


def test_backup_keeps_three(tmp_path: Path) -> None:
    import sqlite3

    from app.services.recorder import backup_sqlite

    db = tmp_path / "p.db"
    sqlite3.connect(db).execute("create table t(x)").connection.commit()
    for i in range(5):
        backup_sqlite(db, tmp_path / "b", DAY + dt.timedelta(days=i))
    assert len(list((tmp_path / "b").glob("platform-*.db"))) == 3
