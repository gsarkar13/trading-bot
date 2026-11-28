"""Fyers live ticks via the SDK data socket (section 14.4).

The SDK runs its own thread and calls `on_message(dict)`. We turn each message into a
`TickData` with the *exchange* timestamp and hand it to a thread-safe callback.

Message fields (fyers-apiv3 3.3.0, FyersWebsocket/map.json):
  equity/option/future ("sf"): ltp, vol_traded_today, last_traded_time, exch_feed_time,
      bid_size, ask_size, bid_price, ask_price, ... (the SDK removes OI from these)
  index ("if"): ltp, prev_close_price, exch_feed_time, high/low/open_price
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from app.adapters.base import InstrumentMappingError, TickData
from app.adapters.fyers.symbols import SymbolMap
from app.core.timeutil import from_epoch

log = logging.getLogger(__name__)


def parse_tick(msg: dict[str, Any], smap: SymbolMap, received: dt.datetime) -> TickData | None:
    """Return a tick for data messages; None for control messages or unknown symbols."""
    if msg.get("type") not in ("sf", "if") or "ltp" not in msg or "symbol" not in msg:
        return None
    try:
        key = smap.key_for(str(msg["symbol"]))
    except InstrumentMappingError:
        log.warning("tick for unmapped symbol %s ignored", msg.get("symbol"))
        return None
    ts = msg.get("exch_feed_time") or msg.get("last_traded_time")
    if not ts:
        return None  # no exchange time: cannot place it in a candle (fail closed)

    def f(name: str) -> Any:
        v = msg.get(name)
        return None if v in (None, 0, 0.0) and name in ("bid_price", "ask_price") else v

    from decimal import Decimal

    def d(v: Any) -> Decimal | None:
        return None if v is None else Decimal(str(v))

    return TickData(
        key=key,
        exchange_ts=from_epoch(int(ts)),
        received_ts=received,
        ltp=Decimal(str(msg["ltp"])),
        bid=d(f("bid_price")),
        ask=d(f("ask_price")),
        bid_qty=msg.get("bid_size"),
        ask_qty=msg.get("ask_size"),
        cum_volume=msg.get("vol_traded_today"),
        oi=None,
    )


class FyersTickStream:
    """Owns one SDK data socket. Callbacks run on the SDK's thread: keep them quick."""

    def __init__(
        self,
        app_id: str,
        access_token: str,
        smap: SymbolMap,
        on_tick: Callable[[TickData], None],
        on_status: Callable[[str, dict[str, Any]], None],
        log_dir: Path = Path("logs"),
        socket_factory: Any = None,
    ) -> None:
        self._smap = smap
        self._on_tick = on_tick
        self._on_status = on_status
        self._symbols: list[str] = []
        self._lock = threading.Lock()
        if socket_factory is None:
            from fyers_apiv3.FyersWebsocket import data_ws

            socket_factory = data_ws.FyersDataSocket
        log_dir.mkdir(parents=True, exist_ok=True)
        self._sock = socket_factory(
            access_token=f"{app_id}:{access_token}",
            log_path=str(log_dir),
            litemode=False,
            write_to_file=False,
            reconnect=True,
            reconnect_retry=50,
            on_connect=self._connected,
            on_close=lambda m: self._on_status("close", m),
            on_error=lambda m: self._on_status("error", m),
            on_message=self._message,
        )

    def _connected(self) -> None:
        self._on_status("connect", {})
        with self._lock:
            if self._symbols:  # resubscribe after every (re)connect (section 14.4)
                self._sock.subscribe(symbols=list(self._symbols), data_type="SymbolUpdate")

    def _message(self, msg: dict[str, Any]) -> None:
        tick = parse_tick(msg, self._smap, dt.datetime.now(dt.UTC))
        if tick is not None:
            self._on_tick(tick)
        elif msg.get("type") not in ("sf", "if"):
            self._on_status("message", msg)

    def subscribe(self, symbols: Sequence[str]) -> None:
        with self._lock:
            new = [s for s in symbols if s not in self._symbols]
            self._symbols.extend(new)
        if new:
            self._sock.subscribe(symbols=new, data_type="SymbolUpdate")

    @property
    def symbols(self) -> list[str]:
        return list(self._symbols)

    def start(self) -> None:
        self._sock.connect()  # SDK starts its own thread

    def stop(self) -> None:
        try:
            self._sock.close_connection()
        except Exception:
            log.exception("error closing Fyers socket")
