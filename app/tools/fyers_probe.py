"""Record one real response from each Fyers data endpoint, with secrets removed.

    python -m app.tools.fyers_probe

Writes reports/probe/<date>/*.json and prints a short summary. Attach that folder (or
zip it) in the chat so the parsers can be checked against real responses. Nothing in
these files is secret: the token and app id are replaced before writing, and the files
are checked with the same detector the tests use (invariant 18).
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.adapters.fyers.session import load_token, make_client, open_vault
from app.adapters.fyers.symbols import SymbolMap, download_master, parse_master
from app.config.loader import load_config
from app.config.secrets import Secrets
from app.core.scrub import find_secrets, redact
from app.core.timeutil import IST
from app.core.types import InstrumentKey


def scrub(obj: Any, secrets_: list[str]) -> str:
    text = json.dumps(obj, indent=1, default=str)
    for s in secrets_:
        if s:
            text = text.replace(s, "<REMOVED>")
    text = redact(text)
    if find_secrets(text):
        raise RuntimeError("secret-like text survived scrubbing; not writing the file")
    return text


def main() -> int:
    cfg = load_config()
    sec = Secrets()
    tok = load_token(open_vault(sec))
    client = make_client(cfg, tok)
    today = dt.datetime.now(IST).date()
    out = Path("reports") / "probe" / today.isoformat()
    out.mkdir(parents=True, exist_ok=True)
    hide = [tok.access_token, tok.app_id]
    summary: dict[str, Any] = {"date": today.isoformat()}

    smap = SymbolMap(parse_master(download_master("NSE_FO") + download_master("NSE_CM")))
    summary["master_instruments"] = len(smap)
    fut = smap.front_future("NIFTY", today, 3)
    start = today - dt.timedelta(days=5)

    calls: dict[str, Any] = {
        "history_index_1m": lambda: client._m.history(
            {  # raw response on purpose
                "symbol": "NSE:NIFTY50-INDEX",
                "resolution": "1",
                "date_format": "1",
                "range_from": start.isoformat(),
                "range_to": today.isoformat(),
                "cont_flag": "0",
            }
        ),
        "history_future_cont_1m": lambda: client._m.history(
            {
                "symbol": smap.symbol(fut),
                "resolution": "1",
                "date_format": "1",
                "range_from": start.isoformat(),
                "range_to": today.isoformat(),
                "cont_flag": "1",
                "oi_flag": "1",
            }
        ),
        "history_vix_daily": lambda: client._m.history(
            {
                "symbol": "NSE:INDIAVIX-INDEX",
                "resolution": "D",
                "date_format": "1",
                "range_from": (today - dt.timedelta(days=30)).isoformat(),
                "range_to": today.isoformat(),
                "cont_flag": "0",
            }
        ),
        "optionchain_nifty": lambda: client._m.optionchain(
            {"symbol": "NSE:NIFTY50-INDEX", "strikecount": 2, "timestamp": ""}
        ),
        "optionchain_banknifty_greeks": lambda: client._m.optionchain(
            {"symbol": "NSE:NIFTYBANK-INDEX", "strikecount": 2, "timestamp": "", "greeks": "1"}
        ),
    }
    for name, fn in calls.items():
        client._limiter.acquire_blocking()
        resp = fn()
        if isinstance(resp, dict) and "candles" in resp:
            resp = {**resp, "candles": resp["candles"][:30], "_total_candles": len(resp["candles"])}
        (out / f"{name}.json").write_text(scrub(resp, hide), encoding="utf-8")
        summary[name] = resp.get("s") if isinstance(resp, dict) else type(resp).__name__
        print(f"  {name}: {summary[name]}")

    # 20 seconds of live socket messages (only meaningful during market hours).
    msgs: list[dict[str, Any]] = []
    from fyers_apiv3.FyersWebsocket import data_ws

    def on_connect() -> None:
        sock.subscribe(
            symbols=[
                "NSE:NIFTY50-INDEX",
                smap.symbol(fut),
                smap.symbol(InstrumentKey.index("INDIAVIX")),
            ],
            data_type="SymbolUpdate",
        )

    Path("logs/fyers").mkdir(parents=True, exist_ok=True)
    sock = data_ws.FyersDataSocket(
        access_token=f"{tok.app_id}:{tok.access_token}",
        log_path="logs/fyers",
        litemode=False,
        reconnect=False,
        on_connect=on_connect,
        on_message=msgs.append,
        on_error=msgs.append,
        on_close=msgs.append,
    )
    threading.Thread(target=sock.connect, daemon=True).start()
    time.sleep(20)
    try:
        sock.close_connection()
    except Exception as e:
        print(f"  note: socket close raised {type(e).__name__}")
    (out / "socket_messages.json").write_text(scrub(msgs[:200], hide), encoding="utf-8")
    summary["socket_messages"] = len(msgs)
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"\nDone. Attach the folder {out} in the chat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
