"""Minimal Telegram Bot API client (long polling, sendMessage, inline buttons).

The bot token is part of every request URL, so URLs and raw errors are never logged
(invariant 18): failures are reported only by type and HTTP status.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx2 as httpx

log = logging.getLogger(__name__)


class TelegramError(Exception):
    pass


class TelegramApi:
    def __init__(self, token: str, timeout_s: float = 35.0) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def _call(self, method: str, body: dict[str, Any]) -> Any:
        try:
            r = await self._client.post(f"{self._base}/{method}", json=body)
        except httpx.HTTPError as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from None
        if r.status_code != 200:
            raise TelegramError(f"{method}: HTTP {r.status_code}")
        d = r.json()
        if not d.get("ok"):
            raise TelegramError(f"{method}: not ok")
        return d.get("result")

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            body["offset"] = offset
        res = await self._call("getUpdates", body)
        return list(res or [])

    async def send(
        self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None
    ) -> None:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        if buttons:
            body["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": t, "callback_data": d[:64]} for t, d in row] for row in buttons
                ]
            }
        await self._call("sendMessage", body)

    async def answer(self, callback_id: str, text: str = "") -> None:
        await self._call(
            "answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]}
        )

    async def close(self) -> None:
        await self._client.aclose()
