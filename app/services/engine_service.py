"""The trading process: recorder + engine in one process, one websocket (Phase 2).

    python -m app.services.engine_service

What it does, in order:
  1. takes the instance lock (invariant 16: two engines can never run on one account)
  2. loads config, the Fyers login and today's instrument master
  3. warms the strategies up on stored candles (the same views the backtest uses)
  4. reconciles every paper book with its saved state (invariant 7)
  5. streams index/VIX/futures ticks (recorded as before) plus the option contracts
     around the money for tradable underlyings (not recorded), and runs the engine
  6. after the close: recorder end-of-day steps, then the journal

Real money stays off in Phase 2: there is no execution-broker adapter yet, so LIVE
always resolves down (invariant 10), and the global ceiling defaults to PAPER.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import app.strategies.plugins  # noqa: F401 - registers the implemented strategies
from app.adapters.base import AuthExpiredError, InstrumentInfo, InstrumentMappingError, TickData
from app.adapters.fyers.symbols import SymbolMap
from app.ai.advisor import Advisor
from app.brokers.paper import Depth, PaperBroker, PaperConfig
from app.brokers.semi import SemiBroker, SemiTask
from app.config.loader import REPO_ROOT, trade_config_hash
from app.config.schema import PlatformConfig
from app.core.hostchecks import ntp_offset_ms, static_ip_ok
from app.core.timeutil import IST, to_ist
from app.core.types import InstrumentKey, OptionType
from app.engine.alerts import AlertBus
from app.engine.contracts import choose_contract
from app.engine.engine import Health, Slot, TradingEngine
from app.engine.history import DbHistory, warmup_views
from app.engine.instance import AlreadyRunning, InstanceLock
from app.engine.journal import write_journal
from app.engine.oms import Alert
from app.research.costs import ChargesTable
from app.strategies.registry import get_plugin

log = logging.getLogger("engine")
WATCH_STRIKES = 2  # option contracts streamed on each side of the money


def build_slots(cfg: PlatformConfig) -> list[Slot]:
    """One slot per configured strategy that has code (D6). Warm-up in 1-minute bars."""
    out = []
    for e in cfg.strategies:
        cls = get_plugin(e.id)
        if cls is None:
            continue
        params = cfg.strategy_params(e.id)
        w = cls.warmup(params)
        need = max(w.bars.get("1m", 0), w.bars.get("15m", 0) * 15)
        out.append(Slot(e.id, cls(params, trade_config_hash(cfg, e.id)), e, need))
    return out


def option_watch(
    smap: SymbolMap, cfg: PlatformConfig, underlying: str, spot: float, now: dt.datetime
) -> list[InstrumentKey]:
    """The contracts the engine may pick in the next minutes: ATM +/- k, CE and PE."""
    c = choose_contract(smap, underlying, "LONG_CE", spot, now, to_ist(now).date(), cfg.contracts)
    if c.key is None or c.key.expiry is None or c.key.strike is None:
        return []
    strikes = smap.strikes(underlying, c.key.expiry)
    i = strikes.index(c.key.strike) if c.key.strike in strikes else 0
    out = []
    for k in strikes[max(0, i - WATCH_STRIKES - 1) : i + WATCH_STRIKES + 2]:
        for ot in (OptionType.CE, OptionType.PE):
            key = InstrumentKey.option(underlying, c.key.expiry, k, ot)
            try:
                smap.info(key)
            except InstrumentMappingError:
                continue  # strike listed for one side only
            out.append(key)
    return out


class EngineService:
    def __init__(
        self,
        cfg: PlatformConfig,
        db: Any,
        smap: SymbolMap,
        client: Any,
        recorder: Any,
        now: Callable[[], dt.datetime],
        state_dir: Path,
        advisor: Advisor | None = None,
    ) -> None:
        self.cfg, self.db, self.smap, self.client, self.rec = cfg, db, smap, client, recorder
        self.now, self.state_dir, self.advisor = now, state_dir, advisor
        self.calendar_dir = REPO_ROOT / "config" / "calendar"
        self.token_expires: dt.datetime | None = None
        self.process_id = uuid.uuid4().hex
        self.alerts = AlertBus(db, cfg.instance.instance_id, now)
        self.health = Health(data_login=True)
        self.queue: asyncio.Queue[TickData] = asyncio.Queue(maxsize=20_000)
        self.engine: TradingEngine | None = None
        self.stop_at = now()

    def _paper(self, book: str, inst: dict[InstrumentKey, InstrumentInfo]) -> PaperBroker:
        safe = book.replace(":", "_")
        return PaperBroker(
            inst,
            self.now,
            PaperConfig(capital=self.cfg.risk.capital_inr),
            state_path=self.state_dir / f"{safe}.json",
        )

    async def _quote(self, key: InstrumentKey) -> Depth | None:
        """REST fallback when no tick has arrived yet for a contract. Fyers quotes carry
        bid/ask but no sizes, so depth stays 0 and the liquidity check fails closed."""
        sym = self.smap.symbol(key)
        try:
            resp = await asyncio.to_thread(self.client.quotes, [sym])
        except Exception as e:
            log.warning("quote %s failed: %s", sym, type(e).__name__)
            return None
        from decimal import Decimal

        for row in resp.get("d", []):
            v = row.get("v", {})
            if row.get("n") == sym and v.get("ask"):
                return Depth(
                    Decimal(str(v.get("bid"))),
                    Decimal(str(v.get("ask"))),
                    0,
                    0,
                    Decimal(str(v.get("lp"))),
                    self.now(),
                )
        return None

    def _on_tick(self, t: TickData) -> None:
        try:
            self.queue.put_nowait(t)
        except asyncio.QueueFull:
            self.alerts.emit("critical", "DATA", "engine tick queue full: ticks dropped")

    async def setup(self) -> TradingEngine:
        today = to_ist(self.now()).date()
        slots = build_slots(self.cfg)
        need_days = max([s.warm_1m for s in slots] + [375]) // 375 + 10
        views, ds = warmup_views(
            self.cfg, self.db, self.rec.underlyings, today, int(need_days * 1.6)
        )
        eng = TradingEngine(
            self.cfg,
            self.db,
            self.smap,
            self.now,
            slots,
            self.alerts,
            ChargesTable(REPO_ROOT / "config" / "charges.yaml"),
            self.process_id,
            self._paper,
            history=DbHistory(self.cfg, self.db, ds, today),
            quote_fetch=self._quote,
            subscribe=lambda k: self.rec.watch([k]),
            health=self.health,
            on_signal=self.advisor.submit if self.advisor else None,
        )

        # SEMI: order tasks go out as alerts (Telegram via the watchdog, and the dashboard)
        def on_task(t: SemiTask, ev: str) -> None:
            urgent = ev in ("stop_requested", "exit_requested", "escalate")
            level: Literal["info", "warn"] = "warn" if urgent else "info"
            self.alerts.emit(
                level, "SEMI", f"{t.uid} {ev}: " + t.message(), {"uid": t.uid, "event": ev}
            )

        def on_alert(a: Alert) -> None:
            self.alerts.emit(a.level, "SEMI", a.message)

        eng.semi = SemiBroker(
            eng.instruments,
            self.now,
            on_task,
            on_alert,
            protect_timeout_s=self.cfg.modes.semi_task_protective_stop_timeout_s,
        )
        for u, vs in views.items():
            for v in vs:
                eng.warm_up(v)
            log.info("%s: warmed up on %d stored candles", u, len(vs))
        for s in slots:
            if s.seen < s.warm_1m:
                self.alerts.emit(
                    "warn",
                    "DATA",
                    f"{s.id}: only {s.seen}/{s.warm_1m} warm-up "
                    "candles stored; its signals are blocked until it is ready",
                )
        await eng.start()
        from app.services.recorder import calendar_problem

        cal = calendar_problem(today, self.calendar_dir)
        if cal is not None:
            eng._halt(cal)  # recorder still records; no new entries (section 14.5)
        self.rec.listeners.append(self._on_tick)
        self.engine = eng
        return eng

    async def _host_checks(self) -> None:
        ic = self.cfg.instance
        while True:
            drift = await asyncio.to_thread(ntp_offset_ms, ic.ntp_server)
            self.health.clock_drift_ms = drift
            if drift is None or abs(drift) > ic.max_clock_drift_ms:
                self.alerts.emit("warn", "HOST", f"clock drift {drift} ms: entries blocked")
            if ic.instance_role.value == "execution":
                ok, why = await asyncio.to_thread(
                    static_ip_ok, ic.registered_static_ip, ic.ip_check_services
                )
                if self.health.static_ip_ok and not ok:
                    self.alerts.emit("critical", "HOST", f"static-IP check failed: {why}")
                self.health.static_ip_ok = ok
            await asyncio.sleep(ic.ip_recheck_minutes * 60)

    async def _consume(self) -> None:
        assert self.engine is not None
        while True:
            t = await self.queue.get()
            try:
                await self.engine.on_tick(t)
            except Exception:
                log.exception("engine tick error")
                self.alerts.emit("critical", "ENGINE", "error while handling a tick; see log")

    async def _timer(self) -> None:
        assert self.engine is not None
        eng = self.engine
        last_watch = 0.0
        while self.now() < self.stop_at:
            try:
                # the data gate's "token expired" check (invariant 6)
                expired = self.token_expires is not None and self.now() >= self.token_expires
                self.health.data_login = not expired and not getattr(
                    self.rec, "chain_disabled", False
                )
                await eng.on_timer()
                if time.monotonic() - last_watch > 60:
                    last_watch = time.monotonic()
                    for u in self.cfg.risk.tradable_underlyings:
                        if u in eng.spot:
                            self.rec.watch(
                                option_watch(self.smap, self.cfg, u, eng.spot[u], self.now())
                            )
            except Exception:
                log.exception("engine timer error")
                self.alerts.emit("critical", "ENGINE", "error in the engine timer; see log")
            await asyncio.sleep(1.0)

    def dashboard_servers(self, password_hash: str | None) -> list[Any]:
        """uvicorn servers for the dashboard, on this event loop (section 15 "Access").
        Extra bind hosts (e.g. the Tailscale IP) are refused without a password."""
        import uvicorn

        from app.api.auth import Auth
        from app.api.server import ApiContext, create_app

        assert self.engine is not None
        d = self.cfg.dashboard
        auth = Auth(password_hash, Path("data") / "local_api.token")
        app = create_app(
            ApiContext(
                self.cfg,
                self.db,
                self.engine,
                auth,
                feeds=lambda: dict(self.rec.stats.socket_events),
            )
        )
        hosts = [d.bind_host]
        if d.extra_bind_hosts:
            if password_hash is None:
                self.alerts.emit(
                    "critical",
                    "HOST",
                    "dashboard NOT opened on "
                    f"{', '.join(d.extra_bind_hosts)}: set a password first "
                    "(python -m app.tools.set_password)",
                )
            else:
                hosts += list(d.extra_bind_hosts)
        return [
            uvicorn.Server(
                uvicorn.Config(app, host=h, port=d.port, log_level="warning", lifespan="off")
            )
            for h in hosts
        ]

    async def run(
        self, stop_at: dt.datetime, password_hash: str | None = None, dashboard: bool = True
    ) -> None:
        self.stop_at = stop_at
        await self.setup()
        coros = [self._consume(), self._host_checks()]
        if dashboard:
            coros += [srv.serve() for srv in self.dashboard_servers(password_hash)]
        tasks = [asyncio.create_task(c) for c in coros]
        try:
            await asyncio.gather(self.rec.run_live(), self._timer())
        finally:
            for t in tasks:
                t.cancel()


def main() -> int:  # pragma: no cover - needs a live Fyers login
    from app.adapters.fyers.session import load_token, make_client, open_vault
    from app.adapters.fyers.symbols import download_master, parse_master
    from app.adapters.fyers.ws import FyersTickStream
    from app.config.loader import load_config
    from app.config.schema import Ema200HammerParams
    from app.config.secrets import Secrets
    from app.core.logsetup import setup_logging
    from app.core.timeutil import ist_dt
    from app.db.session import make_engine
    from app.services.recorder import Recorder, is_trading_day, save_master
    from app.tools.init_db import main as init_db

    setup_logging(Path("logs") / f"engine-{dt.datetime.now(IST):%Y%m%d}.log")
    cfg = load_config()
    today = dt.datetime.now(IST).date()
    ok, why = is_trading_day(today, REPO_ROOT / "config" / "calendar")
    log.info("trading day check: %s", why)
    if not ok:
        return 0
    lock = InstanceLock(Path("data") / f"engine-{cfg.instance.instance_id}.lock")
    try:
        lock.acquire()
    except AlreadyRunning as e:
        log.error("%s", e)
        return 4
    init_db()
    try:
        token = load_token(open_vault(Secrets()))
    except AuthExpiredError as e:
        log.error("%s", e)
        return 2
    client = make_client(cfg, token)
    infos = parse_master(download_master("NSE_FO") + download_master("NSE_CM"))
    smap = SymbolMap(infos)
    db = make_engine(cfg.database.url, cfg.database.sqlite_busy_timeout_ms)
    save_master(db, infos, today)
    s1 = cfg.strategy_params("ema200_hammer_rvol_oi")
    assert isinstance(s1, Ema200HammerParams)
    rc = cfg.recorder
    db_path = Path(cfg.database.url.removeprefix("sqlite:///"))

    def stream_factory(
        on_tick: Callable[[TickData], None], on_status: Callable[[str, dict[str, Any]], None]
    ) -> FyersTickStream:
        return FyersTickStream(
            token.app_id, token.access_token, smap, on_tick, on_status, Path("logs") / "fyers"
        )

    rec = Recorder(
        db,
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
    from app.ai.providers import make_provider

    secrets = Secrets()

    def now() -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    providers = {1: make_provider(cfg.ai.slot1, secrets), 2: make_provider(cfg.ai.slot2, secrets)}
    advisor = Advisor(cfg.ai, db, providers, now)
    svc = EngineService(cfg, db, smap, client, rec, now, Path("data") / "paper", advisor)
    svc.token_expires = token.expires_at
    from app.services.recorder import db_bytes

    start_bytes = db_bytes(db_path)
    try:
        pw = secrets.dashboard_password_hash
        asyncio.run(svc.run(rec.stop_at, pw.get_secret_value() if pw else None))
    except KeyboardInterrupt:
        log.warning("stopped by keyboard")
    except Exception:
        log.exception("engine stopped by an error")
    finally:
        rec.end_of_day(start_bytes)
        stats = write_journal(db, today)
        try:
            asyncio.run(advisor.post_mortem(today, stats))
        except Exception:
            log.exception("AI journal entry failed (advisory only)")
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
