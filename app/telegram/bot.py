"""Telegram commands (brief section 13.4). Only the owner's chat is obeyed (13.1).

Every action goes to the engine over the local link; the bot holds no trading state.
Two-step confirmations: /flatten needs a FLATTEN reply within 60 s; /arm sends a
4-digit code that must be sent back within 2 minutes (D25: same chat, an anti-mistake
step, not protection against a stolen phone).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.telegram.link import EngineDown, EngineLink, EngineRefused

log = logging.getLogger(__name__)
CONFIRM_S = 60


class Sender(Protocol):
    async def send(
        self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None
    ) -> None: ...
    async def answer(self, callback_id: str, text: str = "") -> None: ...


HELP = (
    "/status  /positions  /orders  /strategies\n"
    "/pause  /resume  /mode paper|semi|live\n"
    "/arm  /disarm  /login  /flatten\n"
    "SEMI: /placed T0001 120.5  /stopplaced T0001  /exited T0001 131  /skip T0001\n"
    "Paste the Fyers redirect address here to finish a login."
)


class Bot:
    def __init__(
        self,
        owner_chat_id: int | None,
        tg: Sender,
        link: EngineLink,
        now: Callable[[], dt.datetime],
        emergency_flatten: Callable[[], Awaitable[str]],
        login_link: Callable[[], str | None] = lambda: None,
        finish_login: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self.owner, self.tg, self.link, self.now = owner_chat_id, tg, link, now
        self.emergency_flatten, self.login_link, self.finish_login = (
            emergency_flatten,
            login_link,
            finish_login,
        )
        self._flatten_asked: dt.datetime | None = None
        self._arming = False

    async def say(self, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        if self.owner is not None:
            await self.tg.send(self.owner, text, buttons)

    # ------------------------------------------------------------ dispatch
    async def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            cq = update["callback_query"]
            chat = cq.get("message", {}).get("chat", {}).get("id")
            if chat != self.owner or self.owner is None:
                log.warning("ignored a button press from chat %s", chat)
                return
            await self.tg.answer(cq["id"])
            await self._safe(self.on_button(str(cq.get("data", ""))))
            return
        msg = update.get("message") or {}
        chat = msg.get("chat", {}).get("id")
        text = str(msg.get("text") or "").strip()
        if self.owner is None:
            if text.startswith("/start") and chat is not None:
                await self.tg.send(
                    chat,
                    f"Your chat id is {chat}. Put it in config "
                    "(telegram.owner_chat_id) and restart the watchdog.",
                )
            return
        if chat != self.owner:
            log.warning("ignored a message from chat %s", chat)  # 13.1: logged, not obeyed
            return
        await self._safe(self.on_text(text))

    async def _safe(self, coro: Awaitable[None]) -> None:
        try:
            await coro
        except EngineDown as e:
            await self.say(
                f"Engine not answering ({e}). /flatten still works: it stops the "
                "engine and flattens directly."
            )
        except EngineRefused as e:
            await self.say(f"Refused: {e}")

    async def on_text(self, text: str) -> None:
        if not text:  # a photo, sticker or voice note: nothing to do
            await self.say("I only understand text commands.\n" + HELP)
            return
        low = text.lower()
        if "auth_code=" in text or "code=200" in text:
            if self.finish_login is None:
                await self.say("Logins are not handled here; use `python -m app.tools.login`.")
            else:
                await self.say(await self.finish_login(text))
            return
        if self._flatten_asked and text.strip().upper() == "FLATTEN":
            fresh = (self.now() - self._flatten_asked).total_seconds() <= CONFIRM_S
            self._flatten_asked = None
            if not fresh:
                await self.say("Too late: send /flatten again.")
                return
            await self.say(await self.emergency_flatten())
            return
        if self._arming and re.fullmatch(r"\d{4}", text):
            self._arming = False
            await self.link.post("/api/arm/confirm", {"code": text})
            await self.say("REAL MONEY ARMED for today. /disarm to stop.")
            return
        cmd, *args = low.split()
        if cmd in ("/start", "/help"):
            await self.say(HELP)
        elif cmd == "/status":
            await self.say(format_status(await self.link.get("/api/status")))
        elif cmd in ("/positions", "/orders"):
            s = await self.link.get("/api/status")
            await self.say(format_positions(s))
        elif cmd == "/pause":
            await self.link.post("/api/engine/pause")
            await self.say("New entries paused. Open positions are still managed.")
        elif cmd == "/resume":
            await self.link.post("/api/engine/resume")
            await self.say("New entries allowed again.")
        elif cmd == "/mode" and args and args[0].upper() in ("PAPER", "SEMI", "LIVE"):
            r = await self.link.post("/api/ceiling", {"mode": args[0].upper()})
            await self.say(f"Global ceiling now {r['ceiling']} (real money still needs /arm).")
        elif cmd == "/arm":
            r = await self.link.post("/api/arm/request")
            self._arming = True
            s = r["summary"]
            await self.say(
                f"Arming summary: host {s['host']} ({s['role']}), static IP "
                f"{'ok' if s['static_ip_ok'] else 'NOT ok'}, ceiling {s['ceiling']}."
                f"\nSend this code back within 2 minutes to arm: {r['code']}"
            )
        elif cmd == "/disarm":
            await self.link.post("/api/arm/disarm")
            await self.say("Disarmed.")
        elif cmd == "/strategies":
            await self.send_strategies()
        elif cmd == "/flatten":
            self._flatten_asked = self.now()
            await self.say("EMERGENCY FLATTEN: reply FLATTEN within 60 s to close everything.")
        elif cmd == "/login":
            url = self.login_link()
            await self.say(
                f"Fyers login: {url}\nThen paste the address you land on here."
                if url
                else "Login link unavailable (Fyers app keys not set)."
            )
        elif cmd in ("/placed", "/exited") and len(args) == 2:
            path = "placed" if cmd == "/placed" else "exited"
            await self.link.post(f"/api/semi/{args[0].upper()}/{path}", {"price": args[1]})
            await self.say(f"{args[0].upper()}: {path} at {args[1]} recorded.")
        elif cmd in ("/stopplaced", "/skip") and len(args) == 1:
            path = "stop-placed" if cmd == "/stopplaced" else "skip"
            await self.link.post(f"/api/semi/{args[0].upper()}/{path}")
            await self.say(f"{args[0].upper()}: {path} recorded.")
        else:
            await self.say(HELP)

    async def send_strategies(self) -> None:
        rows = await self.link.get("/api/strategies")
        for r in rows:
            if r["code_status"] != "implemented":
                continue
            sid = r["id"]
            await self.say(
                f"#{r['priority']} {r['name']}\non: {r['active']} · requested "
                f"{r['requested_mode']} · effective {r['effective_mode']} · gate "
                f"{r['gate_status']}",
                [
                    [
                        (
                            "off" if r["active"] else "on",
                            f"s:{'off' if r['active'] else 'on'}:{sid}",
                        ),
                        ("▲", f"s:up:{sid}"),
                        ("▼", f"s:down:{sid}"),
                    ],
                    [(m, f"s:mode:{sid}:{m}") for m in ("PAPER", "SEMI", "LIVE")],
                ],
            )

    async def on_button(self, data: str) -> None:
        parts = data.split(":")
        if parts[0] != "s" or len(parts) < 3:
            return
        what, sid = parts[1], parts[2]
        if what in ("on", "off"):
            await self.link.post(f"/api/strategies/{sid}/active", {"active": what == "on"})
        elif what in ("up", "down"):
            await self.link.post(f"/api/strategies/{sid}/move", {"up": what == "up"})
        elif what == "mode" and len(parts) == 4:
            await self.link.post(f"/api/strategies/{sid}/mode", {"mode": parts[3]})
        await self.say(f"{sid}: {what} done (invariant 17 checked by the engine).")


def format_status(s: dict[str, Any]) -> str:
    h = s["host"]
    br = s["breakers"]
    lines = [
        f"{s['now_ist'][11:19]} IST · {h['label']} ({h['role']}) · static IP "
        f"{'ok' if h['static_ip_ok'] else 'not ok'}",
        f"engine: {'PAUSED - ' + s['engine']['halted'] if s['engine']['halted'] else 'running'}"
        f" · ceiling {s['ceiling']} · armed {s['armed']}",
        "prices: " + ", ".join(f"{u} {p:.2f}" for u, p in s["spot"].items()),
        f"P&L today: paper {s['pnl']['paper']['net']} ({s['pnl']['paper']['trades']} trades),"
        f" real {s['pnl']['real']['net']}",
        "breakers: " + ("; ".join(br["PAPER"]["reasons"] + br["REAL"]["reasons"]) or "ok"),
        f"open positions: {len([p for p in s['positions'] if not p['book'].startswith('SHADOW')])}",
    ]
    return "\n".join(lines)


def format_positions(s: dict[str, Any]) -> str:
    ps = [p for p in s["positions"] if not p["book"].startswith("SHADOW")]
    out = [
        f"{p['book']} {p['strategy']} {p['key'].replace('|', ' ')} x{p['qty']} "
        f"{p['state']} stop {p['stop']:.2f}"
        for p in ps
    ]
    tasks = [
        f"SEMI {t['uid']} {t['status']}: {t['symbol']} x{t['qty']} @ {t['limit']}"
        for t in s["semi_tasks"]
    ]
    return "\n".join(out + tasks) or "No open positions or tasks."
