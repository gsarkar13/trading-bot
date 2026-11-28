"""Fyers REST data calls through the official SDK (`fyers-apiv3`), rate limited.

Only data endpoints live here (history, option chain, quotes). Order endpoints come in
Phase 3. Every response is checked: Fyers returns HTTP 200 with {"s": "error"} for many
failures, so status codes alone are not enough.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.adapters.base import (
    AdapterError,
    AuthExpiredError,
    InstrumentMappingError,
    RateLimitedError,
)
from app.core.ratelimit import RateLimiter
from app.core.timeutil import from_epoch
from app.data.bars import Bar

AUTH_CODES = {-8, -15, -16, -17}
RATE_CODES = {-429, 429}
SYMBOL_CODES = {-300}
MAX_INTRADAY_DAYS = 100  # per request (BROKER_NOTES.md)
MAX_DAILY_DAYS = 366


def check(resp: Any, what: str) -> dict[str, Any]:
    if not isinstance(resp, dict):
        raise AdapterError(f"{what}: unexpected response type {type(resp).__name__}")
    if resp.get("s") == "ok":
        return resp
    code = resp.get("code")
    msg = str(resp.get("message", ""))[:200]
    if code in AUTH_CODES:
        raise AuthExpiredError(f"{what}: Fyers auth error {code}: {msg}")
    if code in RATE_CODES:
        raise RateLimitedError(f"{what}: Fyers rate limit: {msg}")
    if code in SYMBOL_CODES or "invalid symbol" in msg.lower():
        raise InstrumentMappingError(f"{what}: {msg}")
    raise AdapterError(f"{what}: Fyers error {code}: {msg}")


def parse_candles(resp: dict[str, Any]) -> list[Bar]:
    out = []
    for row in resp.get("candles") or []:
        ts, o, h, low, c, v = row[:6]
        oi = int(row[6]) if len(row) > 6 and row[6] is not None else None
        out.append(Bar(from_epoch(ts), float(o), float(h), float(low), float(c), int(v or 0), oi))
    return out


def chunks(start: dt.date, end: dt.date, days: int) -> list[tuple[dt.date, dt.date]]:
    out, a = [], start
    while a <= end:
        b = min(end, a + dt.timedelta(days=days - 1))
        out.append((a, b))
        a = b + dt.timedelta(days=1)
    return out


class FyersClient:
    """Thin synchronous wrapper. The recorder runs it in a worker thread."""

    def __init__(
        self,
        app_id: str,
        access_token: str,
        limiter: RateLimiter,
        log_dir: Path = Path("logs"),
        model_factory: Callable[..., Any] | None = None,
    ) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        if model_factory is None:
            from fyers_apiv3 import fyersModel

            model_factory = fyersModel.FyersModel
        self._m = model_factory(
            client_id=app_id,
            token=access_token,
            is_async=False,
            log_path=str(log_dir),
            log_level="ERROR",
        )
        self._limiter = limiter

    def _call(
        self, what: str, fn: Callable[[dict[str, Any]], Any], data: dict[str, Any]
    ) -> dict[str, Any]:
        self._limiter.acquire_blocking()
        return check(fn(data), what)

    def history(
        self,
        symbol: str,
        resolution: str,
        start: dt.date,
        end: dt.date,
        cont_flag: bool = False,
        oi_flag: bool = False,
    ) -> list[Bar]:
        limit = MAX_DAILY_DAYS if resolution in ("D", "1D") else MAX_INTRADAY_DAYS
        if (end - start).days + 1 > limit:
            raise ValueError(f"history range > {limit} days; split it with chunks()")
        data = {
            "symbol": symbol,
            "resolution": resolution,
            "date_format": "1",
            "range_from": start.isoformat(),
            "range_to": end.isoformat(),
            "cont_flag": "1" if cont_flag else "0",
            "oi_flag": "1" if oi_flag else "0",
        }
        return parse_candles(self._call(f"history {symbol}", self._m.history, data))

    def option_chain(
        self, symbol: str, strikecount: int, expiry_epoch: int | None = None
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "symbol": symbol,
            "strikecount": strikecount,
            "timestamp": str(expiry_epoch) if expiry_epoch else "",
        }
        return self._call(f"optionchain {symbol}", self._m.optionchain, data)

    def quotes(self, symbols: Sequence[str]) -> dict[str, Any]:
        return self._call("quotes", self._m.quotes, {"symbols": ",".join(symbols)})
