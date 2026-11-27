"""The whole trading-day routine for Phase 1, in one command:

    python -m app.tools.daily

1. Asks for your vault passphrase once (kept in memory for this run only).
2. If today's Fyers token is missing or expired, runs the login (paste the redirect URL).
3. Runs the recorder until 15:35 IST, then the end-of-day downloads and the summary.
Leave the window open all day. Closing it stops the recorder.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

from app.adapters.base import AuthExpiredError
from app.config.secrets import Secrets
from app.core.vault import ENV_VAR, Vault, get_passphrase


def main() -> int:
    from pathlib import Path

    from app.adapters.fyers.session import load_token
    from app.services import recorder
    from app.tools import login

    sec = Secrets()
    vault_path = Path(sec.vault_path)
    os.environ[ENV_VAR] = get_passphrase(confirm_new=not vault_path.exists())  # this process only
    try:
        tok = load_token(Vault(vault_path, os.environ[ENV_VAR]), dt.datetime.now(dt.UTC))
        print(f"Fyers token valid until {tok.expires_at:%Y-%m-%d %H:%M} UTC; skipping login.")
    except AuthExpiredError:
        rc = login.main()
        if rc != 0:
            return rc
    return recorder.main()


if __name__ == "__main__":
    sys.exit(main())
