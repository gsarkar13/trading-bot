"""The watchdog's local channel to the engine: its own HTTP API on 127.0.0.1, with the
random token the engine wrote to `data/local_api.token` (section 13.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2 as httpx


class EngineDown(Exception):
    """The engine did not answer (stopped, hung or starting)."""


class EngineRefused(Exception):
    """The engine answered with an error (e.g. a refused change); the text says why."""


class EngineLink:
    def __init__(self, port: int, token_path: Path, timeout_s: float = 5.0) -> None:
        self.base, self.token_path, self.timeout_s = (
            f"http://127.0.0.1:{port}",
            token_path,
            timeout_s,
        )

    def _headers(self) -> dict[str, str]:
        try:
            tok = self.token_path.read_text(encoding="utf-8").strip()
        except OSError:
            raise EngineDown("no engine token file (engine not started?)") from None
        return {"X-Local-Token": tok}

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as c:
                r = await c.request(method, self.base + path, json=body, headers=self._headers())
        except httpx.HTTPError as e:
            raise EngineDown(type(e).__name__) from None
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise EngineRefused(str(detail))
        return r.json()

    async def get(self, path: str) -> Any:
        return await self.call("GET", path)

    async def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return await self.call("POST", path, body or {})
