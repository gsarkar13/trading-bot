"""The Phase 2 trading day in one command (Windows: deploy\\windows_trade.bat).

1. Asks for the vault passphrase once (kept in memory, passed only to the child process).
2. Logs in to Fyers if today's token is missing or expired (paste the redirect address).
3. Starts the watchdog (Telegram + heartbeat + emergency flatten) if Telegram is set up.
4. Runs the engine: recorder + strategies + paper trading + dashboard at
   http://127.0.0.1:8600 until 15:35 IST, then the end-of-day steps and the journal.
Leave the window open all day. Closing it stops trading; open positions keep their
broker-side stops (paper books: they are simply resumed on the next start).
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

from app.adapters.base import AuthExpiredError
from app.config.secrets import Secrets
from app.core.timeutil import IST
from app.core.vault import ENV_VAR, Vault, get_passphrase


def main() -> int:  # pragma: no cover - interactive, needs a Fyers login
    from app.adapters.fyers.session import load_token
    from app.config.loader import load_config
    from app.services import engine_service
    from app.tools import login

    cfg = load_config()
    sec = Secrets()
    vault_path = Path(sec.vault_path)
    os.environ[ENV_VAR] = get_passphrase(confirm_new=not vault_path.exists())  # this run only
    try:
        tok = load_token(Vault(vault_path, os.environ[ENV_VAR]), dt.datetime.now(dt.UTC))
        print(f"Fyers token valid until {tok.expires_at.astimezone(IST):%H:%M} IST.")
    except AuthExpiredError:
        if login.main() != 0:
            print("Login failed: nothing started.")
            return 1
    if sec.dashboard_password_hash is None:
        print(
            "Note: no dashboard password set (python -m app.tools.set_password). The "
            "dashboard opens on this PC only and real-money actions stay off."
        )
    watchdog = None
    if cfg.telegram.enabled and sec.telegram_bot_token is not None:
        watchdog = subprocess.Popen([sys.executable, "-m", "app.services.watchdog"])
        print("Watchdog started (Telegram).")
    print(
        f"\nDashboard: http://{cfg.dashboard.bind_host}:{cfg.dashboard.port}\n"
        "Leave this window open until ~15:40.\n"
    )
    try:
        return engine_service.main()
    finally:
        if watchdog is not None:
            watchdog.terminate()


if __name__ == "__main__":
    sys.exit(main())
