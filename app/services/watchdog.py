"""Watchdog (brief section 13.2): a small separate process that

* owns Telegram polling and relays the engine's alerts from `event_log`
* watches the engine heartbeat (`instances.last_seen_at`) and alerts when it stops
* sends the 08:45 login reminder on trading days
* on /flatten: asks the engine first; if the engine doesn't answer, stops the engine
  process (PID from its lock file) and runs the standalone flatten tool itself

  python -m app.services.watchdog
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.config.schema import PlatformConfig
from app.core.timeutil import IST, to_ist
from app.db.models import EventLog, Instance
from app.engine.instance import AlreadyRunning, InstanceLock
from app.telegram.bot import Bot, Sender
from app.telegram.link import EngineDown, EngineLink, EngineRefused
from app.tools.flatten import FlattenReport, QuoteFn, flatten_paper_books

log = logging.getLogger("watchdog")
ICON = {"critical": "🔴", "warn": "🟠", "info": "🔵"}


def default_kill(pid: int) -> None:  # pragma: no cover - real processes
    os.kill(pid, signal.SIGTERM)


class Watchdog:
    def __init__(
        self,
        cfg: PlatformConfig,
        db: Engine,
        sender: Sender,
        link: EngineLink,
        now: Callable[[], dt.datetime],
        lock_path: Path,
        paper_dir: Path,
        quote: QuoteFn,
        is_trading_day: Callable[[dt.date], bool] = lambda d: d.weekday() < 5,
        kill: Callable[[int], None] = default_kill,
        flatten_offline: Callable[..., Awaitable[list[FlattenReport]]] = flatten_paper_books,
        login_link: Callable[[], str | None] = lambda: None,
        finish_login: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self.cfg, self.db, self.link, self.now = cfg, db, link, now
        self.lock_path, self.paper_dir, self.quote = lock_path, paper_dir, quote
        self.is_trading_day, self.kill, self.flatten_offline = is_trading_day, kill, flatten_offline
        self.bot = Bot(
            cfg.telegram.owner_chat_id,
            sender,
            link,
            now,
            self.emergency_flatten,
            login_link,
            finish_login,
        )
        with Session(db) as s:
            self.last_event_id = int(s.scalar(select(func.max(EventLog.id))) or 0)
        self.heartbeat_alerted = False
        self.reminded_on: dt.date | None = None

    # ------------------------------------------------------------ alerts out
    def _wanted(self, e: EventLog) -> bool:
        if e.severity in ("critical", "warn"):
            return True
        if e.category == "SEMI":
            return True
        if e.category == "TRADE":
            book = e.message.split(" ", 1)[0]
            if book.startswith("SHADOW:"):
                return False
            return not (self.cfg.telegram.mute_paper_trade_alerts and book.startswith("PAPER"))
        return False

    async def relay_alerts(self) -> int:
        with Session(self.db) as s:
            rows = list(
                s.scalars(
                    select(EventLog)
                    .where(EventLog.id > self.last_event_id)
                    .order_by(EventLog.id)
                    .limit(50)
                )
            )
        n = 0
        for e in rows:
            self.last_event_id = e.id
            if self._wanted(e):
                await self.bot.say(f"{ICON.get(e.severity, '')} {e.category}: {e.message}")
                n += 1
        return n

    # ------------------------------------------------------------ heartbeat
    def _session(self, now: dt.datetime) -> bool:
        t = to_ist(now)
        r = self.cfg.recorder
        return self.is_trading_day(t.date()) and r.start <= t.time() <= r.stop

    async def check_heartbeat(self) -> None:
        now = self.now()
        if not self._session(now):
            return
        with Session(self.db) as s:
            row = s.get(Instance, self.cfg.instance.instance_id)
        seen = row.last_seen_at if row else None
        if seen is not None and seen.tzinfo is None:
            seen = seen.replace(tzinfo=dt.UTC)
        limit = self.cfg.telegram.engine_heartbeat_timeout_s
        stale = seen is None or (now - seen).total_seconds() > limit
        if stale and not self.heartbeat_alerted:
            self.heartbeat_alerted = True
            age = "never" if seen is None else f"{(now - seen).total_seconds():.0f}s ago"
            await self.bot.say(
                f"🔴 ENGINE HEARTBEAT LOST (last {age}). Positions keep their "
                "broker-side stops. /flatten works from here."
            )
        elif not stale and self.heartbeat_alerted:
            self.heartbeat_alerted = False
            await self.bot.say("🔵 Engine heartbeat is back.")

    async def login_reminder(self) -> None:
        t = to_ist(self.now())
        if (
            self.is_trading_day(t.date())
            and self.reminded_on != t.date()
            and t.time() >= self.cfg.telegram.login_reminder
            and t.hour < 15
        ):
            self.reminded_on = t.date()
            url = self.bot.login_link()
            await self.bot.say(
                "Good morning. Daily login: "
                + (url or "run the login tool")
                + "\nPaste the address you land on here. Without it the "
                "engine runs PAPER only once the data login is done."
            )

    # ------------------------------------------------------------ emergency flatten
    async def emergency_flatten(self) -> str:
        try:
            r = await self.link.post("/api/flatten", {"phrase": "FLATTEN"})
            return (
                f"🔴 FLATTEN sent by the engine for {r['positions']} position(s). "
                "Watch for the closes."
            )
        except EngineRefused as e:
            return f"Engine refused flatten: {e}"
        except EngineDown as e:
            log.critical("engine not answering (%s): stopping it and flattening directly", e)
        pid = self._engine_pid()
        if pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                self.kill(pid)
        lock = InstanceLock(self.lock_path)
        for _ in range(20):
            try:
                lock.acquire()
                break
            except AlreadyRunning:
                await asyncio.sleep(0.5)
        else:
            return (
                "🔴 Engine hung and could not be stopped. EXIT EVERY POSITION BY HAND in "
                "the broker app now."
            )
        try:
            reps = await self.flatten_offline(
                self.paper_dir,
                self.db,
                self.cfg.instance.instance_id,
                to_ist(self.now()).date(),
                self.quote,
                self.now,
            )
        finally:
            lock.release()
        lines = [ln for r in reps for ln in r.lines()] or ["no books found"]
        head = "🔴 Engine was hung: stopped it and flattened directly."
        if any(r.still_open for r in reps):
            head += " SOME POSITIONS ARE STILL OPEN: exit them by hand."
        return head + "\n" + "\n".join(lines)

    def _engine_pid(self) -> int | None:
        for f in (self.lock_path.with_suffix(".pid"), self.lock_path):
            try:
                return int(f.read_text(encoding="utf-8").strip().splitlines()[0])
            except (OSError, ValueError, IndexError):
                continue
        return None

    # ------------------------------------------------------------ loop
    async def tick(self) -> None:
        for step in (self.relay_alerts, self.check_heartbeat, self.login_reminder):
            try:
                await step()
            except Exception:
                log.exception("watchdog step %s failed", step.__name__)


async def _poll(wd: Watchdog, tg: object) -> None:  # pragma: no cover - network
    from app.telegram.api import TelegramApi, TelegramError

    assert isinstance(tg, TelegramApi)
    offset: int | None = None
    while True:
        try:
            for u in await tg.get_updates(offset):
                offset = int(u["update_id"]) + 1  # confirmed even if handling fails
                try:
                    await wd.bot.handle(u)
                except Exception:  # one odd message must never stop /flatten (inv 11)
                    log.exception("could not handle a Telegram update")
        except TelegramError as e:
            log.warning("telegram: %s", e)
            await asyncio.sleep(5)
        except Exception:
            log.exception("telegram polling error")
            await asyncio.sleep(5)


async def _ticker(wd: Watchdog) -> None:  # pragma: no cover - loop
    while True:
        await wd.tick()
        await asyncio.sleep(2)


def main() -> int:  # pragma: no cover - needs Telegram
    from app.config.loader import REPO_ROOT, load_config
    from app.config.secrets import Secrets
    from app.core.logsetup import setup_logging
    from app.db.session import make_engine
    from app.services.recorder import is_trading_day
    from app.telegram.api import TelegramApi
    from app.tools.flatten import _fyers_quote_fn

    setup_logging(Path("logs") / f"watchdog-{dt.datetime.now(IST):%Y%m%d}.log")
    cfg = load_config()
    sec = Secrets()
    if not cfg.telegram.enabled or sec.telegram_bot_token is None:
        log.error("Telegram is off: set telegram.enabled and ALGO_TELEGRAM_BOT_TOKEN")
        return 2
    db = make_engine(cfg.database.url, cfg.database.sqlite_busy_timeout_ms)
    tg = TelegramApi(sec.telegram_bot_token.get_secret_value())
    link = EngineLink(cfg.dashboard.port, Path("data") / "local_api.token")

    async def quote(key):  # type: ignore[no-untyped-def]
        return await _fyers_quote_fn(cfg)(key)

    wd = Watchdog(
        cfg,
        db,
        tg,
        link,
        lambda: dt.datetime.now(dt.UTC),
        Path("data") / f"engine-{cfg.instance.instance_id}.lock",
        Path("data") / "paper",
        quote,
        is_trading_day=lambda d: is_trading_day(d, REPO_ROOT / "config" / "calendar")[0],
        login_link=_login_link_fn(sec),
        finish_login=_finish_login_fn(cfg, sec),
    )

    async def both() -> None:
        await asyncio.gather(_poll(wd, tg), _ticker(wd))

    asyncio.run(both())
    return 0


_LOGIN_STATE: dict[str, str] = {}


def _login_link_fn(sec: object) -> Callable[[], str | None]:  # pragma: no cover
    from app.adapters.fyers import auth
    from app.config.secrets import Secrets

    assert isinstance(sec, Secrets)

    def link() -> str | None:
        if not (sec.fyers_app_id and sec.fyers_redirect_uri):
            return None
        _LOGIN_STATE["state"] = auth.new_state()
        return auth.login_url(
            sec.fyers_app_id.get_secret_value(), sec.fyers_redirect_uri, _LOGIN_STATE["state"]
        )

    return link


def _finish_login_fn(cfg: PlatformConfig, sec: object) -> Callable[[str], Awaitable[str]]:
    # pragma: no cover - needs Fyers
    from app.config.secrets import Secrets
    from app.tools.login import finish_login

    assert isinstance(sec, Secrets)

    async def done(pasted: str) -> str:
        try:
            exp = await asyncio.to_thread(finish_login, cfg, sec, pasted, _LOGIN_STATE.get("state"))
        except Exception as e:
            return f"Login failed: {type(e).__name__}: {e}"
        return (
            f"Logged in. Token valid until {to_ist(exp):%H:%M} IST tomorrow. Restart the "
            "engine if it was waiting for the login."
        )

    return done


if __name__ == "__main__":
    sys.exit(main())
