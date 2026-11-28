"""Market-data recorder (section 16.1). Runs 09:00-15:35 IST on trading days.

    python -m app.services.recorder

During the session it records:
  * ticks for NIFTY, BANKNIFTY, India VIX and the front-month futures (raw, kept 30 days)
  * 1-minute candles built live from those ticks (source "fyers_ws", used by replay)
  * option-chain snapshots, ATM +/- n strikes, every `chain_interval_s` (kept forever)
After the close it:
  * downloads the day's official 1-minute and daily candles by REST (source
    "fyers_hist", the same source as backfill, so research data is consistent)
  * downloads 1-minute candles for every option contract seen in the day's snapshots
    (DECISIONS.md D22: the "real option P&L" layer)
  * prunes raw ticks past retention, backs up the database, and writes
    reports/recorder/<date>.json (counts, gaps, disk use and a 12-month projection)

Option contracts are not streamed: their candles come from REST after the close, which
keeps the tick table small (DECISIONS.md D28).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import sqlite3
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Engine, delete, func, select, text
from sqlalchemy.orm import Session

from app.adapters.base import AuthExpiredError, ChainSnapshot, TickData
from app.adapters.fyers.chain import parse_chain
from app.adapters.fyers.symbols import SymbolMap
from app.core.timeutil import IST, ist_dt, to_ist
from app.core.types import InstrumentKey, OptionType
from app.data.bars import Bar
from app.data.candle_builder import CandleBuilder
from app.data.store import upsert_bars
from app.db.models import Instrument, OptionChainSnapshot, Tick

log = logging.getLogger("recorder")

WS_SOURCE, HIST_SOURCE = "fyers_ws", "fyers_hist"
UNDERLYING_SYMBOL = {"NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX"}


class DataClient(Protocol):
    def history(
        self,
        symbol: str,
        resolution: str,
        start: dt.date,
        end: dt.date,
        cont_flag: bool = False,
        oi_flag: bool = False,
    ) -> list[Bar]: ...

    def option_chain(
        self, symbol: str, strikecount: int, expiry_epoch: int | None = None
    ) -> dict[str, Any]: ...


class TickStream(Protocol):
    def subscribe(self, symbols: list[str]) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


@dataclass
class DayStats:
    ticks: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    ws_candles: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    snapshots: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    chain_errors: int = 0
    socket_events: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    stale_alerts: list[str] = field(default_factory=list)
    option_keys: dict[str, InstrumentKey] = field(default_factory=dict)


def trading_expiries(smap: SymbolMap, underlying: str, today: dt.date) -> list[dt.date]:
    """Expiries to snapshot: the nearest one, plus the next on its expiry day (D24)."""
    exps = [e for e in smap.option_expiries(underlying) if e >= today]
    if not exps:
        return []
    return exps[:2] if exps[0] == today else exps[:1]


def save_master(
    engine: Engine,
    smap_infos: list[Any],
    master_date: dt.date,
    underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY"),
) -> int:
    """Store today's master, but only rows we can use: our underlyings' futures and
    options, indices and cash equities. The full file (~90k rows) would add ~13 MB a day."""
    keep = [
        i
        for i in smap_infos
        if i.key.kind in ("INDEX", "EQ")
        or (i.key.kind in ("OPT", "FUT") and i.key.underlying in underlyings)
    ]
    with Session(engine) as s:
        if s.scalar(
            select(func.count(Instrument.id)).where(
                Instrument.provider == "fyers", Instrument.master_date == master_date
            )
        ):
            return 0
        for i in keep:
            k = i.key
            s.add(
                Instrument(
                    key=k.canonical,
                    provider="fyers",
                    master_date=master_date,
                    broker_symbol=i.broker_symbol,
                    broker_token=i.broker_token,
                    kind=k.kind,
                    underlying=k.underlying,
                    expiry=k.expiry,
                    strike=k.strike,
                    option_type=k.option_type.value if k.option_type else None,
                    isin=k.isin,
                    series=k.series,
                    lot_size=i.lot_size,
                    tick_size=i.tick_size,
                )
            )
        s.commit()
    return len(keep)


def store_snapshot(engine: Engine, snap: ChainSnapshot) -> int:
    sid = f"{snap.underlying}-{snap.expiry:%Y%m%d}-{uuid.uuid4().hex[:8]}"
    with Session(engine) as s:
        for r in snap.rows:
            s.add(
                OptionChainSnapshot(
                    snapshot_id=sid,
                    taken_at=snap.taken_at,
                    underlying=snap.underlying,
                    expiry=snap.expiry,
                    spot=snap.spot,
                    india_vix=snap.india_vix,
                    strike=r.strike,
                    option_type=r.option_type.value,
                    instrument_key=r.key.canonical,
                    bid=r.bid,
                    ask=r.ask,
                    ltp=r.ltp,
                    volume=r.volume,
                    oi=r.oi,
                    oi_change=r.oi_change,
                    iv=r.iv,
                    delta=r.delta,
                    source="fyers",
                )
            )
        s.commit()
    return len(snap.rows)


def store_ticks(engine: Engine, ticks: list[TickData]) -> None:
    if not ticks:
        return
    with Session(engine) as s:
        s.add_all(
            Tick(
                instrument_key=t.key.canonical,
                exchange_ts=t.exchange_ts,
                received_ts=t.received_ts,
                ltp=t.ltp,
                bid=t.bid,
                ask=t.ask,
                bid_qty=t.bid_qty,
                ask_qty=t.ask_qty,
                cum_volume=t.cum_volume,
                oi=t.oi,
                source="fyers",
            )
            for t in ticks
        )
        s.commit()


def prune_ticks(engine: Engine, now: dt.datetime, retention_days: int) -> int:
    with Session(engine) as s:
        res = s.execute(
            delete(Tick).where(Tick.exchange_ts < now - dt.timedelta(days=retention_days))
        )
        s.commit()
        return int(getattr(res, "rowcount", 0) or 0)


def backup_sqlite(db_path: Path, backup_dir: Path, day: dt.date, keep: int = 3) -> Path:
    """Online backup with SQLite's .backup API (section 16.1); keeps the newest `keep`."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"platform-{day:%Y%m%d}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in sorted(backup_dir.glob("platform-*.db"))[:-keep]:
        old.unlink()
    return dest


def sqlite_quick_check(db_path: Path) -> str:
    con = sqlite3.connect(db_path)
    try:
        return str(con.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        con.close()


def db_bytes(db_path: Path | None) -> int:
    if db_path is None:
        return 0
    return sum(
        p.stat().st_size for p in (db_path, db_path.with_name(db_path.name + "-wal")) if p.exists()
    )


class Recorder:
    def __init__(
        self,
        engine: Engine,
        client: DataClient,
        smap: SymbolMap,
        stream_factory: Callable[
            [Callable[[TickData], None], Callable[[str, dict[str, Any]], None]], TickStream
        ],
        *,
        day: dt.date,
        underlyings: tuple[str, ...],
        chain_interval_s: float,
        n_strikes: int,
        start: dt.datetime,
        stop: dt.datetime,
        db_path: Path | None,
        retention_days: int = 30,
        roll_days: int = 3,
        report_dir: Path = Path("reports") / "recorder",
        backup_dir: Path = Path("data") / "backups",
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        stale_after_s: float = 60,
    ) -> None:
        self.engine, self.client, self.smap = engine, client, smap
        self.day, self.underlyings = day, underlyings
        self.chain_interval_s, self.n_strikes = chain_interval_s, n_strikes
        self.start_at, self.stop_at = start, stop
        self.db_path, self.retention_days, self.roll_days = db_path, retention_days, roll_days
        self.report_dir, self.backup_dir, self.now = report_dir, backup_dir, now
        self.stale_after_s = stale_after_s
        self.stats = DayStats()
        self.builders: dict[InstrumentKey, CandleBuilder] = {}
        self.last_tick: dict[InstrumentKey, dt.datetime] = {}
        self._pending: list[TickData] = []
        self._stream_factory = stream_factory
        self._expiry_epoch: dict[tuple[str, dt.date], int] = {}
        self.chain_disabled = False
        self.loop_errors = 0
        self.keys = self._tick_keys()
        # Extra consumers of every tick (the trading engine). Called on the event loop.
        self.listeners: list[Callable[[TickData], None]] = []
        self.stream: TickStream | None = None
        self._extra: set[InstrumentKey] = set()

    def _tick_keys(self) -> list[InstrumentKey]:
        keys = [InstrumentKey.index(u) for u in self.underlyings]
        keys.append(InstrumentKey.index("INDIAVIX"))
        keys += [self.smap.front_future(u, self.day, self.roll_days) for u in self.underlyings]
        return keys

    # ---------------------------------------------------------------- live part
    def _on_tick(self, loop: asyncio.AbstractEventLoop) -> Callable[[TickData], None]:
        def cb(t: TickData) -> None:
            loop.call_soon_threadsafe(self._accept, t)

        return cb

    def watch(self, keys: list[InstrumentKey]) -> None:
        """Stream more instruments for the listeners (not recorded as ticks or candles)."""
        new = [k for k in keys if k not in self._extra and k not in self.builders]
        if not new:
            return
        self._extra.update(new)
        if self.stream is not None:
            self.stream.subscribe([self.smap.symbol(k) for k in new])

    def _accept(self, t: TickData) -> None:
        for fn in self.listeners:
            try:
                fn(t)
            except Exception:
                log.exception("tick listener failed")
        if t.key not in self.builders:
            return
        self._pending.append(t)
        self.stats.ticks[t.key.canonical] += 1
        self.last_tick[t.key] = t.received_ts
        for b in self.builders[t.key].add(t):
            self._write_candle(t.key, b)

    def _on_status(self, kind: str, msg: dict[str, Any]) -> None:
        self.stats.socket_events[kind] += 1
        if kind == "error":
            log.warning("socket error: %s", str(msg)[:200])

    def _write_candle(self, key: InstrumentKey, b: Bar) -> None:
        upsert_bars(self.engine, key.canonical, "1m", WS_SOURCE, [b])
        self.stats.ws_candles[key.canonical] += 1

    def snapshot_chains(self) -> None:
        if self.chain_disabled:
            return
        for u in self.underlyings:
            for exp in trading_expiries(self.smap, u, self.day):
                try:
                    epoch = self._expiry_epoch.get((u, exp))
                    resp = self.client.option_chain(UNDERLYING_SYMBOL[u], self.n_strikes, epoch)
                    snap = parse_chain(resp, u, self.smap, self.now())
                    for e_date, e_epoch in _expiry_epochs(resp):
                        self._expiry_epoch[(u, e_date)] = e_epoch
                    if snap.expiry != exp and epoch is None:
                        # first call returns the nearest expiry; retry with the epoch now known
                        epoch = self._expiry_epoch.get((u, exp))
                        if epoch is None:
                            continue
                        snap = parse_chain(
                            self.client.option_chain(UNDERLYING_SYMBOL[u], self.n_strikes, epoch),
                            u,
                            self.smap,
                            self.now(),
                        )
                    store_snapshot(self.engine, snap)
                    self.stats.snapshots[f"{u} {exp}"] += 1
                    self.stats.option_keys.update({r.key.canonical: r.key for r in snap.rows})
                except AuthExpiredError as e:
                    # Keep recording ticks; stop asking for chains until a new login.
                    self.chain_disabled = True
                    self.stats.chain_errors += 1
                    log.error(
                        "token rejected by option-chain call; chain snapshots paused "
                        "(ticks keep recording). Log in again and restart: %s",
                        e,
                    )
                    return
                except Exception as e:
                    self.stats.chain_errors += 1
                    log.warning("chain %s %s failed: %s: %s", u, exp, type(e).__name__, e)

    def check_stale(self) -> None:
        now = self.now()
        for k in self.keys:
            last = self.last_tick.get(k)
            if last is None or (now - last).total_seconds() > self.stale_after_s:
                msg = (
                    f"{to_ist(now):%H:%M:%S} no tick for {k.canonical} in {self.stale_after_s:.0f}s"
                )
                if not self.stats.stale_alerts or self.stats.stale_alerts[-1] != msg:
                    self.stats.stale_alerts.append(msg)
                    log.warning(msg)

    async def run_live(self) -> None:
        loop = asyncio.get_running_loop()
        while self.now() < self.start_at:
            await asyncio.sleep(min(30.0, (self.start_at - self.now()).total_seconds()))
        self.builders = {k: CandleBuilder() for k in self.keys}
        stream = self._stream_factory(self._on_tick(loop), self._on_status)
        self.stream = stream
        stream.start()
        stream.subscribe([self.smap.symbol(k) for k in self.keys])
        if self._extra:
            stream.subscribe([self.smap.symbol(k) for k in self._extra])
        next_chain = time.monotonic()
        next_stale = time.monotonic() + self.stale_after_s
        try:
            while self.now() < self.stop_at:
                try:
                    next_chain, next_stale = await self._tick_once(next_chain, next_stale)
                except Exception as e:
                    self.loop_errors += 1
                    log.exception("recorder loop error #%d: %s", self.loop_errors, e)
                await asyncio.sleep(1.0 if self.chain_interval_s >= 1 else 0.05)
        finally:
            stream.stop()
            try:
                store_ticks(self.engine, self._pending)
            except Exception:
                log.exception("could not store the last %d ticks", len(self._pending))
            self._pending = []
            for k, b in self.builders.items():
                for bar in b.flush():
                    self._write_candle(k, bar)

    async def _tick_once(self, next_chain: float, next_stale: float) -> tuple[float, float]:
        """One pass of the live loop: store ticks, close candles, snapshot, staleness."""
        if self._pending:
            batch, self._pending = self._pending, []
            try:
                await asyncio.to_thread(store_ticks, self.engine, batch)
            except Exception:
                self._pending = batch + self._pending  # retry on the next pass
                raise
        for k, b in self.builders.items():
            for bar in b.close_until(self.now()):
                self._write_candle(k, bar)
        if time.monotonic() >= next_chain:
            await asyncio.to_thread(self.snapshot_chains)
            next_chain = time.monotonic() + self.chain_interval_s
        if time.monotonic() >= next_stale:
            self.check_stale()
            next_stale = time.monotonic() + 10
        return next_chain, next_stale

    # ---------------------------------------------------------------- after the close
    def _snapped_option_keys(self) -> dict[str, InstrumentKey]:
        a = ist_dt(self.day, dt.time(0, 0)).astimezone(dt.UTC).replace(tzinfo=None)
        q = text(
            "SELECT DISTINCT instrument_key FROM option_chain_snapshots "
            "WHERE taken_at >= :a AND taken_at < :b"
        )
        out = {}
        with self.engine.connect() as c:
            for (canon,) in c.execute(
                q, {"a": a.isoformat(" "), "b": (a + dt.timedelta(days=1)).isoformat(" ")}
            ):
                k = _parse_option_key(canon)
                if k is not None and k.underlying in self.underlyings:
                    out[canon] = k
        return out

    def end_of_day(self, bytes_at_start: int) -> dict[str, Any]:
        rest: dict[str, int] = {}
        series: list[tuple[InstrumentKey, str, bool, str]] = []
        for u in self.underlyings:
            series.append((InstrumentKey.index(u), UNDERLYING_SYMBOL[u], False, f"INDEX|{u}"))
            fut = self.smap.front_future(u, self.day, self.roll_days)
            series.append((fut, self.smap.symbol(fut), True, f"FUTC|{u}"))
            series.append((fut, self.smap.symbol(fut), False, fut.canonical))
        series.append(
            (InstrumentKey.index("INDIAVIX"), "NSE:INDIAVIX-INDEX", False, "INDEX|INDIAVIX")
        )
        for _key, sym, cont, store_key in series:
            for tf, res in (("1m", "1"), ("1d", "D")):
                try:
                    bars = self.client.history(sym, res, self.day, self.day, cont_flag=cont)
                    rest[f"{store_key} {tf}"] = upsert_bars(
                        self.engine, store_key, tf, HIST_SOURCE, bars
                    )
                except Exception as e:
                    rest[f"{store_key} {tf}"] = -1
                    log.warning("EOD history %s failed: %s", store_key, e)
        # Contracts snapped today, from the database, so a mid-day restart loses nothing.
        try:
            self.stats.option_keys.update(self._snapped_option_keys())
        except Exception:
            log.exception("could not read today's snapshot contracts")
        options_ok = 0
        for canon, k in sorted(self.stats.option_keys.items()):
            try:
                bars = self.client.history(
                    self.smap.symbol(k), "1", self.day, self.day, oi_flag=True
                )
                upsert_bars(self.engine, canon, "1m", HIST_SOURCE, bars)
                options_ok += 1
            except Exception as e:
                log.warning("EOD option history %s failed: %s", canon, e)
        pruned = -1
        try:
            pruned = prune_ticks(self.engine, self.now(), self.retention_days)
        except Exception:
            log.exception("tick pruning failed")
        backup = None
        try:
            if self.db_path is not None and self.db_path.exists():
                backup = str(backup_sqlite(self.db_path, self.backup_dir, self.day))
        except Exception:
            log.exception("backup failed")
        used = db_bytes(self.db_path) - bytes_at_start
        gaps = {k.canonical: len(b.stats.gap_minutes) for k, b in self.builders.items()}
        summary = {
            "date": self.day.isoformat(),
            "ticks": dict(self.stats.ticks),
            "ws_candles": dict(self.stats.ws_candles),
            "ws_gap_minutes": gaps,
            "late_ticks": {k.canonical: b.stats.late for k, b in self.builders.items()},
            "duplicate_ticks": {k.canonical: b.stats.duplicates for k, b in self.builders.items()},
            "chain_snapshots": dict(self.stats.snapshots),
            "chain_errors": self.stats.chain_errors,
            "chain_paused_by_auth_error": self.chain_disabled,
            "loop_errors": self.loop_errors,
            "option_contracts_seen": len(self.stats.option_keys),
            "option_contracts_with_rest_candles": options_ok,
            "rest_candles": rest,
            "socket_events": dict(self.stats.socket_events),
            "stale_alerts": self.stats.stale_alerts[:100],
            "ticks_pruned": pruned,
            "backup": backup,
            "db_bytes_added_today": used,
            "projection_12_months_gb": round(used * 250 / 1e9, 2),
            "server_disk_gb": 40,
        }
        self.report_dir.mkdir(parents=True, exist_ok=True)
        (self.report_dir / f"{self.day}.json").write_text(
            json.dumps(summary, indent=1), encoding="utf-8"
        )
        return summary


def _parse_option_key(canon: str) -> InstrumentKey | None:
    try:
        _, u, exp, strike, ot = canon.split("|")
        return InstrumentKey.option(u, dt.date.fromisoformat(exp), strike, OptionType(ot))
    except ValueError:
        return None


def _expiry_epochs(resp: dict[str, Any]) -> list[tuple[dt.date, int]]:
    data = resp.get("data", resp)
    out = []
    for e in data.get("expiryData") or []:
        if e.get("expiry"):
            ep = int(e["expiry"])
            out.append((dt.datetime.fromtimestamp(ep, IST).date(), ep))
    return out


def is_trading_day(d: dt.date, calendar_dir: Path) -> tuple[bool, str]:
    if d.weekday() >= 5:
        return False, "weekend"
    f = calendar_dir / f"nse_{d.year}.yaml"
    if f.exists():
        import yaml

        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        hols = {str(h["date"]) for h in data.get("holidays") or []}
        if d.isoformat() in hols:
            return False, "NSE holiday (calendar file)"
    return True, "ok" if f.exists() else "ok (no calendar file; weekday assumed open)"


def main() -> int:
    from app.adapters.fyers.session import load_token, make_client, open_vault
    from app.adapters.fyers.symbols import download_master, parse_master
    from app.adapters.fyers.ws import FyersTickStream
    from app.config.loader import REPO_ROOT, load_config
    from app.config.secrets import Secrets
    from app.db.session import make_engine
    from app.tools.init_db import main as init_db

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(_log_file(), encoding="utf-8")],
    )
    cfg = load_config()
    today = dt.datetime.now(IST).date()
    ok, why = is_trading_day(today, REPO_ROOT / "config" / "calendar")
    log.info("trading day check: %s", why)
    if not ok:
        return 0
    init_db()
    try:
        token = load_token(open_vault(Secrets()))
    except AuthExpiredError as e:
        log.error("%s", e)
        return 2
    client = make_client(cfg, token)
    infos = parse_master(download_master("NSE_FO") + download_master("NSE_CM"))
    smap = SymbolMap(infos)
    engine = make_engine(cfg.database.url, cfg.database.sqlite_busy_timeout_ms)
    log.info(
        "symbol master: %d instruments (%d new rows saved)",
        len(smap),
        save_master(engine, infos, today),
    )
    db_path = (
        Path(cfg.database.url.removeprefix("sqlite:///"))
        if cfg.database.url.startswith("sqlite:///")
        else None
    )
    if db_path is not None and db_path.exists():
        integrity = sqlite_quick_check(db_path)  # ARCHITECTURE F9
        log.info("database integrity check: %s", integrity)
        if integrity != "ok":
            log.error("database damaged; restore the newest file from data/backups/ (RUNBOOK)")
            return 3
    rc = cfg.recorder
    from app.config.schema import Ema200HammerParams

    s1 = cfg.strategy_params("ema200_hammer_rvol_oi")
    assert isinstance(s1, Ema200HammerParams)

    def stream_factory(
        on_tick: Callable[[TickData], None], on_status: Callable[[str, dict[str, Any]], None]
    ) -> FyersTickStream:
        return FyersTickStream(
            token.app_id, token.access_token, smap, on_tick, on_status, Path("logs") / "fyers"
        )

    rec = Recorder(
        engine,
        client,
        smap,
        stream_factory,
        day=today,
        underlyings=rc.underlyings,
        chain_interval_s=float(s1.chain_interval_s),
        n_strikes=s1.chain_n_strikes,
        roll_days=s1.futures_roll_days_before_expiry,
        start=ist_dt(today, rc.start),
        stop=ist_dt(today, rc.stop),
        db_path=db_path,
        retention_days=rc.raw_tick_retention_days,
    )
    start_bytes = db_bytes(db_path)
    if dt.datetime.now(dt.UTC) < rec.stop_at:
        log.info("recording %s until %s IST", [k.canonical for k in rec.keys], rc.stop)
        try:
            asyncio.run(rec.run_live())
        except KeyboardInterrupt:
            log.warning("stopped by keyboard; running the end-of-day steps now")
        except Exception:
            log.exception("live recording stopped by an error; running end-of-day steps")
    summary = rec.end_of_day(start_bytes)
    log.info(
        "done: %s",
        json.dumps({k: summary[k] for k in ("ticks", "chain_snapshots", "db_bytes_added_today")}),
    )
    print(f"\nDaily summary written to reports/recorder/{today}.json; attach it in the chat.")
    return 0


def _log_file() -> Path:
    p = Path("logs")
    p.mkdir(exist_ok=True)
    return p / f"recorder-{dt.datetime.now(IST):%Y%m%d}.log"


if __name__ == "__main__":
    sys.exit(main())
