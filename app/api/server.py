"""FastAPI app: the dashboard's JSON API, a WebSocket status push and the static UI.

Runs on the engine's event loop. Handlers only read engine state or call short engine
methods; nothing here blocks tick processing for long (section 14.1).

Security (section 15):
  * binds to 127.0.0.1 by default; Tailscale IP via `dashboard.extra_bind_hosts`
  * login with a scrypt-hashed password; real-money actions need a fresh login
  * every POST needs the `X-Algo: 1` header (no CORS is enabled, so another site
    can't send it: CSRF guard)
  * secrets are never returned: the settings view shows only whether a key is set and
    its last 4 characters
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

import app.strategies.plugins  # noqa: F401 - registers implemented strategies
from app.ai.advisor import what_if
from app.api.auth import Auth
from app.config.schema import PlatformConfig
from app.config.secrets import Secrets
from app.core.timeutil import IST, to_ist
from app.core.types import Mode
from app.data.resample import resample
from app.data.store import load_bars
from app.db.models import AIVerdict, EventLog, JournalEOD, RiskState, SignalRow, Trade
from app.engine.engine import TradingEngine
from app.engine.journal import book_stats
from app.engine.risk import Breakers
from app.engine.strategy_table import ChangeRefused, StrategyTable
from app.indicators.core import EMA
from app.research.dataset import SOURCES
from app.strategies.registry import code_status

STATIC = Path(__file__).parent / "static"


@dataclass
class ApiContext:
    cfg: PlatformConfig
    db: Engine
    engine: TradingEngine
    auth: Auth
    feeds: Callable[[], dict[str, Any]] = lambda: {}
    started_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


class Pw(BaseModel):
    password: str


class Code(BaseModel):
    code: str


class Phrase(BaseModel):
    phrase: str


class StopAction(BaseModel):
    action: str


class Price(BaseModel):
    price: Decimal


class Active(BaseModel):
    active: bool


class ModeBody(BaseModel):
    mode: Mode


class Move(BaseModel):
    up: bool


def _ist(t: dt.datetime | None) -> str | None:
    return None if t is None else to_ist(t).isoformat(timespec="seconds")


def _today_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    a = dt.datetime.combine(to_ist(now).date(), dt.time(0), IST).astimezone(dt.UTC)
    return a, a + dt.timedelta(days=1)


def _mask(v: Any) -> str | None:
    if v is None:
        return None
    s = v.get_secret_value() if hasattr(v, "get_secret_value") else str(v)
    return f"set (...{s[-4:]})" if len(s) >= 8 else "set"


def create_app(ctx: ApiContext) -> FastAPI:
    app = FastAPI(title="algo", docs_url=None, redoc_url=None, openapi_url=None)
    eng, db, cfg, auth = ctx.engine, ctx.db, ctx.cfg, ctx.auth
    table = StrategyTable(db, eng.is_exposed, cfg.instance.instance_id)

    # ------------------------------------------------------------ auth plumbing
    def who(request: Request) -> str:
        host = request.client.host if request.client else None
        if auth.local_ok(request.headers.get("x-local-token"), host):
            return "telegram"
        if auth.valid(request.cookies.get("sid")) is None:
            raise HTTPException(401, "log in first")
        if request.method == "POST" and request.headers.get("x-algo") != "1":
            raise HTTPException(403, "missing X-Algo header")
        return "dashboard"

    def fresh(request: Request) -> None:
        if who(request) == "telegram":
            return
        if not auth.fresh(request.cookies.get("sid")):
            raise HTTPException(403, "real-money actions need a fresh login (password again)")

    @app.post("/api/login")
    def login(body: Pw, response: Response) -> dict[str, Any]:
        sid = auth.login(body.password)
        if sid is None:
            raise HTTPException(401, "wrong password, or locked for 5 minutes")
        response.set_cookie("sid", sid, httponly=True, samesite="strict", max_age=12 * 3600)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response) -> dict[str, Any]:
        auth.logout(request.cookies.get("sid"))
        response.delete_cookie("sid")
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request) -> dict[str, Any]:
        sid = request.cookies.get("sid")
        return {
            "logged_in": auth.valid(sid) is not None,
            "password_set": auth.required,
            "fresh": auth.fresh(sid),
        }

    # ------------------------------------------------------------ status
    def breakers(book: str) -> dict[str, Any]:
        b = Breakers(db, cfg.risk, book, cfg.instance.instance_id)
        st = b.check(eng.now(), eng._unrealized(book))
        with Session(db) as s:
            rows = list(s.scalars(select(RiskState).where(RiskState.book == book)))
        day = max(
            (r for r in rows if r.period == "day"), key=lambda r: r.period_start, default=None
        )
        return {
            "tripped": st.tripped,
            "reasons": st.reasons,
            "today_realized": str(day.realized_pnl) if day else "0",
            "trades_today": day.trades if day else 0,
        }

    def status() -> dict[str, Any]:
        now = eng.now()
        a, b = _today_bounds(now)
        with Session(db) as s:
            trades = list(s.scalars(select(Trade).where(Trade.exit_at >= a, Trade.exit_at < b)))

        def pnl(pred: Callable[[str], bool]) -> dict[str, Any]:
            ts = [t for t in trades if pred(t.book)]
            w = sum(1 for t in ts if t.net_pnl > 0)
            return {
                "net": str(sum((t.net_pnl for t in ts), Decimal(0))),
                "trades": len(ts),
                "win_rate": round(w / len(ts), 2) if ts else None,
            }

        h, ic = eng.health, cfg.instance
        live_why = []
        if ic.instance_role.value != "execution":
            live_why.append("research instance")
        if not h.static_ip_ok:
            live_why.append("static-IP check not passed")
        if eng.live is None:
            live_why.append("no execution broker in this build (Phase 3)")
        return {
            "now_ist": _ist(now),
            "engine": {"halted": eng.state.halted, "signals_today": eng.state.signals},
            "armed": eng.arming.is_armed(now),
            "ceiling": eng.ceiling.value,
            "config_ceiling": cfg.modes.global_ceiling.value,
            "host": {
                "label": ic.host_label,
                "role": ic.instance_role.value,
                "instance_id": ic.instance_id,
                "static_ip_ok": h.static_ip_ok,
                "clock_drift_ms": h.clock_drift_ms,
                "live_possible": not live_why,
                "live_blockers": live_why,
            },
            "feeds": ctx.feeds(),
            "last_tick": {u: _ist(t) for u, t in eng.last_tick_at.items()},
            "spot": eng.spot,
            "vix": eng.vix,
            "order_rate": {n: r.limiter.recent() for n, r in eng.routers.items()},
            "pnl": {
                "paper": pnl(lambda bk: bk == "PAPER"),
                "real": pnl(lambda bk: bk == "REAL"),
                "shadow": pnl(lambda bk: bk.startswith("SHADOW:")),
            },
            "breakers": {"PAPER": breakers("PAPER"), "REAL": breakers("REAL")},
            "positions": eng.snapshot()["positions"],
            "semi_tasks": [
                {
                    "uid": t.uid,
                    "symbol": t.symbol,
                    "qty": t.qty,
                    "limit": str(t.entry_limit),
                    "status": t.status,
                    "stop": [str(x) for x in t.planned_stop] if t.planned_stop else None,
                    "message": t.message(),
                    "created": _ist(t.created),
                }
                for t in (eng.semi.tasks.values() if eng.semi else [])
                if t.status not in ("EXITED", "SKIPPED", "EXPIRED")
            ],
            "alerts": [
                {"at": _ist(m.at), "level": m.level, "category": m.category, "message": m.message}
                for m in list(eng.alerts.recent)[-20:]
            ],
        }

    @app.get("/api/status")
    def get_status(_: str = Depends(who)) -> dict[str, Any]:
        return status()

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        if auth.valid(socket.cookies.get("sid")) is None:
            await socket.close(code=4401)
            return
        await socket.accept()
        try:
            while True:
                await socket.send_json(status())
                await asyncio.sleep(1.0)
        except Exception:  # client went away
            return

    # ------------------------------------------------------------ controls
    @app.post("/api/engine/pause")
    def pause(by: str = Depends(who)) -> dict[str, Any]:
        eng.pause(by)
        return {"halted": eng.state.halted}

    @app.post("/api/engine/resume")
    def resume(by: str = Depends(who)) -> dict[str, Any]:
        eng.resume(by)
        return {"halted": eng.state.halted}

    @app.post("/api/flatten")
    async def flatten(body: Phrase, by: str = Depends(who)) -> dict[str, Any]:
        if body.phrase.strip().upper() != "FLATTEN":
            raise HTTPException(400, "type FLATTEN to confirm")
        n = await eng.flatten_all("emergency button", by)
        return {"positions": n}

    @app.post("/api/positions/{uid}/stop")
    def stop(uid: str, body: StopAction, by: str = Depends(who)) -> dict[str, Any]:
        try:
            return {"stop": eng.adjust_stop(uid, body.action, by)}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/ceiling")
    def ceiling(body: ModeBody, request: Request, by: str = Depends(who)) -> dict[str, Any]:
        if body.mode.is_real_money:
            fresh(request)
        try:
            eng.set_ceiling(body.mode, by)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ceiling": eng.ceiling.value}

    @app.post("/api/arm/request")
    def arm_request(request: Request, by: str = Depends(who)) -> dict[str, Any]:
        fresh(request)
        code = eng.arming.request(eng.now(), by)
        summary = eng.arming_summary()
        eng.alerts.emit("warn", "RISK", f"arming requested by {by}: confirm with the code")
        return {"code": code, "summary": summary, "expires_s": 120}

    @app.post("/api/arm/confirm")
    def arm_confirm(body: Code, request: Request, by: str = Depends(who)) -> dict[str, Any]:
        fresh(request)
        ok = eng.arming.confirm(eng.now(), body.code, by, eng.arming_summary())
        if not ok:
            raise HTTPException(400, "wrong or expired code")
        return {"armed": True}

    @app.post("/api/arm/disarm")
    def disarm(by: str = Depends(who)) -> dict[str, Any]:
        eng.arming.disarm(eng.now(), f"by {by}")
        return {"armed": False}

    @app.post("/api/breakers/{book}/reset")
    def reset(book: str, body: Phrase, request: Request, by: str = Depends(who)) -> dict[str, Any]:
        fresh(request)
        b = Breakers(db, cfg.risk, book, cfg.instance.instance_id)
        if not b.reset(eng.now(), body.phrase, by):
            raise HTTPException(400, f"type exactly: {Breakers.reset_phrase(eng.now())}")
        return {"ok": True}

    # ------------------------------------------------------------ SEMI tasks
    def semi() -> Any:
        if eng.semi is None:
            raise HTTPException(404, "SEMI is not running")
        return eng.semi

    async def pump() -> None:
        for r in eng.routers.values():
            await r.pump()

    @app.post("/api/semi/{uid}/placed")
    async def semi_placed(uid: str, body: Price, by: str = Depends(who)) -> dict[str, Any]:
        semi().confirm_entry(uid, body.price)
        await pump()
        return {"ok": True}

    @app.post("/api/semi/{uid}/stop-placed")
    async def semi_stop(uid: str, by: str = Depends(who)) -> dict[str, Any]:
        semi().confirm_stop(uid)
        await pump()
        return {"ok": True}

    @app.post("/api/semi/{uid}/exited")
    async def semi_exit(uid: str, body: Price, by: str = Depends(who)) -> dict[str, Any]:
        semi().confirm_exit(uid, body.price)
        await pump()
        return {"ok": True}

    @app.post("/api/semi/{uid}/skip")
    async def semi_skip(uid: str, by: str = Depends(who)) -> dict[str, Any]:
        semi().skip(uid)
        await pump()
        return {"ok": True}

    # ------------------------------------------------------------ strategies
    def rows() -> list[dict[str, Any]]:
        out = []
        for r in table.rows():
            slot = next((s for s in eng.slots if s.id == r.id), None)
            mode, why = eng._mode_for(slot, r) if slot else (Mode.PAPER, ("no code",))
            out.append(
                {
                    "id": r.id,
                    "priority": r.priority,
                    "name": r.name,
                    "segment": r.segment,
                    "stage": r.stage,
                    "code_status": code_status(r.id).value,  # derived live (D6)
                    "active": r.active,
                    "requested_mode": r.requested_mode,
                    "effective_mode": mode.value,
                    "effective_reasons": list(why),
                    "gate_status": r.gate_status,
                    "gate_passed_on": r.gate_passed_on.isoformat() if r.gate_passed_on else None,
                    "notes": r.notes,
                    "version": r.version,
                    "config_hash": r.config_hash,
                    "warm": None
                    if slot is None
                    else f"{min(slot.seen, slot.warm_1m)}/{slot.warm_1m}",
                    "exposed": eng.is_exposed(r.id),
                }
            )
        return out

    @app.get("/api/strategies")
    def strategies(_: str = Depends(who)) -> list[dict[str, Any]]:
        return rows()

    def change(fn: Callable[[], Any]) -> list[dict[str, Any]]:
        try:
            fn()
        except ChangeRefused as e:
            raise HTTPException(400, str(e)) from e
        return rows()

    @app.post("/api/strategies/{sid}/active")
    def set_active(sid: str, body: Active, by: str = Depends(who)) -> list[dict[str, Any]]:
        return change(lambda: table.set_active(sid, body.active, eng.now(), by))

    @app.post("/api/strategies/{sid}/mode")
    def set_mode(
        sid: str, body: ModeBody, request: Request, by: str = Depends(who)
    ) -> list[dict[str, Any]]:
        if body.mode.is_real_money:
            fresh(request)
        return change(lambda: table.set_mode(sid, body.mode, eng.now(), by))

    @app.post("/api/strategies/{sid}/move")
    def move(sid: str, body: Move, by: str = Depends(who)) -> list[dict[str, Any]]:
        return change(lambda: table.move(sid, body.up, eng.now(), by))

    # ------------------------------------------------------------ data views
    @app.get("/api/events")
    def events(
        category: str | None = None, limit: int = 200, _: str = Depends(who)
    ) -> list[dict[str, Any]]:
        q = select(EventLog).order_by(EventLog.id.desc()).limit(min(limit, 1000))
        if category:
            q = q.where(EventLog.category == category.upper())
        with Session(db) as s:
            return [
                {
                    "at": _ist(e.at),
                    "category": e.category,
                    "severity": e.severity,
                    "message": e.message,
                }
                for e in s.scalars(q)
            ]

    @app.get("/api/signals")
    def signals(limit: int = 100, _: str = Depends(who)) -> list[dict[str, Any]]:
        with Session(db) as s:
            sigs = list(
                s.scalars(select(SignalRow).order_by(SignalRow.id.desc()).limit(min(limit, 500)))
            )
            ai = {
                v.signal_uid: v
                for v in s.scalars(
                    select(AIVerdict).where(
                        AIVerdict.signal_uid.in_([x.signal_uid for x in sigs]),
                        AIVerdict.status == "OK",
                    )
                )
            }
        return [
            {
                "uid": x.signal_uid,
                "at": _ist(x.created_at),
                "strategy": x.strategy_id,
                "underlying": x.underlying,
                "direction": x.direction,
                "outcome": x.outcome,
                "checklist": x.checklist,
                "inputs": x.inputs,
                "ai": None
                if x.signal_uid not in ai
                else {
                    "approve": ai[x.signal_uid].approve,
                    "confidence": ai[x.signal_uid].confidence,
                    "reason": ai[x.signal_uid].reason,
                },
            }
            for x in sigs
        ]

    @app.get("/api/trades")
    def trades(limit: int = 200, _: str = Depends(who)) -> list[dict[str, Any]]:
        with Session(db) as s:
            ts = list(s.scalars(select(Trade).order_by(Trade.id.desc()).limit(min(limit, 2000))))
        return [
            {
                "id": t.id,
                "strategy": t.strategy_id,
                "book": t.book,
                "instrument": t.instrument_key,
                "entry_at": _ist(t.entry_at),
                "exit_at": _ist(t.exit_at),
                "qty": t.qty,
                "entry": str(t.entry_price),
                "exit": str(t.exit_price),
                "gross": str(t.gross_pnl),
                "charges": t.charges,
                "charges_total": str(t.charges_total),
                "net": str(t.net_pnl),
                "compliance": t.compliance_score,
                "overrides": t.overrides,
                "self_reported": t.self_reported,
            }
            for t in ts
        ]

    @app.get("/api/books")
    def books(_: str = Depends(who)) -> dict[str, Any]:
        return {"books": book_stats(db), "ai_what_if": what_if(db)}

    @app.get("/api/journal")
    def journal(_: str = Depends(who)) -> list[dict[str, Any]]:
        with Session(db) as s:
            js = list(
                s.scalars(select(JournalEOD).order_by(JournalEOD.trading_date.desc()).limit(30))
            )
        return [
            {
                "date": j.trading_date.isoformat(),
                "stats": j.stats,
                "ai_review": j.ai_review,
                "ai_provider": j.ai_provider,
                "notes": j.trader_notes,
            }
            for j in js
        ]

    @app.get("/api/candles")
    def candles(
        u: str = "NIFTY", tf: int = 1, days: int = 2, _: str = Depends(who)
    ) -> dict[str, Any]:
        if u not in cfg.recorder.underlyings or tf not in (1, 15):
            raise HTTPException(400, "unknown underlying or timeframe")
        today = to_ist(eng.now()).date()
        a = today - dt.timedelta(days=max(1, min(days, 30)) + (40 if tf == 15 else 0))
        bars = load_bars(db, f"INDEX|{u}", "1m", SOURCES, a, today)
        if tf == 15:
            bars = [b for b, complete in resample(bars, 15) if complete]
        ema, out_ema = EMA(200), []
        for b in bars:
            v = ema.update(b.close)
            if ema.ready:
                out_ema.append({"time": int(b.ts.timestamp()), "value": round(v, 2)})
        keep = int(
            dt.datetime.combine(today - dt.timedelta(days=days), dt.time(0), IST).timestamp()
        )
        pd = eng.history.prior_day(u, today)
        return {
            "bars": [
                {
                    "time": int(b.ts.timestamp()),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                }
                for b in bars
                if b.ts.timestamp() >= keep
            ],
            "ema200": [e for e in out_ema if e["time"] >= keep],
            "pdh": pd.high if pd else None,
            "pdl": pd.low if pd else None,
        }

    @app.get("/api/settings")
    def settings(_: str = Depends(who)) -> dict[str, Any]:
        sec = Secrets()
        keys = {
            k: _mask(getattr(sec, k))
            for k in (
                "fyers_app_id",
                "fyers_app_secret",
                "flattrade_api_key",
                "flattrade_api_secret",
                "telegram_bot_token",
                "gemini_api_key",
                "anthropic_api_key",
                "openai_api_key",
                "openrouter_api_key",
            )
        }
        c = cfg.model_dump(mode="json")
        return {
            "keys": keys,
            "slots": {
                "ai_1": c["ai"]["slot1"],
                "ai_2": c["ai"]["slot2"],
                "data_1": "fyers",
                "data_2": None,
                "exec_1": "flattrade (Phase 3)",
                "exec_2": "fyers (Phase 3)",
            },
            "routing": c["routing"],
            "risk": c["risk"],
            "time_guards": c["time_guards"],
            "contracts": c["contracts"],
            "gate": c["gate"],
            "strategies": c["strategies"],
            "notes": [
                "Settings are edited in config/*.yaml and .env, then the engine is "
                "restarted (D56). Keys are never sent to the browser.",
                "Ollama is unlikely to fit next to the engine in 1 GB of RAM.",
            ],
        }

    # ------------------------------------------------------------ static UI
    if (STATIC / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC / "index.html")
    else:

        @app.get("/")
        def no_ui() -> JSONResponse:
            return JSONResponse({"error": "dashboard not built; see docs/DASHBOARD.md"})

    return app
