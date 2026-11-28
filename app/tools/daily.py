"""The whole trading-day routine for Phase 1, in one command:

    python -m app.tools.daily        (Windows: double-click deploy\\windows_daily.bat)

1. Asks for your vault passphrase once (kept in memory for this run only).
2. If today's Fyers token is missing or expired, runs the login (paste the redirect URL).
3. Runs the recorder until 15:35 IST, then the end-of-day downloads and the summary.
4. Replays the day from the recorded ticks.
5. Packs everything into reports/phase1_bundle_<time>.zip  <- attach this file.
Leave the window open all day. Closing it stops the recorder.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from typing import Any

from app.adapters.base import AuthExpiredError
from app.config.secrets import Secrets
from app.core.timeutil import IST
from app.core.vault import ENV_VAR, Vault, get_passphrase


def main() -> int:
    from app.adapters.fyers.session import load_token
    from app.config.loader import load_config
    from app.db.session import make_engine
    from app.services import recorder
    from app.tools import login, replay
    from app.tools.phase1 import (
        REPORTS,
        environment_report,
        known_secret_values,
        make_bundle,
        run_step,
    )

    sec = Secrets()
    vault_path = Path(sec.vault_path)
    os.environ[ENV_VAR] = get_passphrase(confirm_new=not vault_path.exists())  # this process only
    steps: list[dict[str, Any]] = []

    def ensure_login() -> int:
        try:
            tok = load_token(Vault(vault_path, os.environ[ENV_VAR]), dt.datetime.now(dt.UTC))
            print(
                f"Fyers token valid until {tok.expires_at.astimezone(IST):%H:%M} IST; "
                "skipping login."
            )
            return 0
        except AuthExpiredError:
            return login.main()

    if run_step(steps, "Fyers login", ensure_login):
        today = dt.datetime.now(IST).date()
        if run_step(steps, "record the trading day", recorder.main):
            run_step(
                steps,
                "replay the day from ticks",
                lambda: replay.main(["--date", today.isoformat()]),
            )
    cfg = load_config()
    engine = make_engine(cfg.database.url, cfg.database.sqlite_busy_timeout_ms)
    bundle, _ = make_bundle(
        REPORTS, steps, environment_report(engine), known_secret_values(), dt.datetime.now(IST)
    )
    print(f"\nAttach this ONE file in the chat:\n  {bundle}")
    return 0 if all(s["ok"] for s in steps) else 1


if __name__ == "__main__":
    sys.exit(main())
