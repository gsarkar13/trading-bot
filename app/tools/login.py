"""Daily Fyers login (section 12). Run once each trading day, after 06:30 IST.

    python -m app.tools.login

1. Opens nothing by itself: it prints a login link. Open it in any browser, log in to
   Fyers and complete 2FA.
2. Fyers redirects to your redirect URL. Copy the FULL address from the browser's address
   bar and paste it here. (On the Fyers default redirect page this works from a phone too.)
3. The token is exchanged on THIS machine and saved encrypted in the vault.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sys

from app.adapters.fyers import auth
from app.adapters.fyers.session import VAULT_KEY, open_vault
from app.config.loader import load_config
from app.config.schema import PlatformConfig
from app.config.secrets import Secrets
from app.core.timeutil import IST
from app.core.vault import Vault
from app.db.models import LoginSession
from app.db.session import make_engine, make_session_factory


def finish_login(
    cfg: PlatformConfig,
    sec: Secrets,
    pasted: str,
    state: str | None,
    vault: Vault | None = None,
) -> dt.datetime:
    """Exchange the pasted redirect (or bare code) for a token ON THIS MACHINE, store it
    encrypted, record the login. Returns the expiry. Used by this tool and Telegram."""
    assert sec.fyers_app_id and sec.fyers_app_secret and sec.fyers_redirect_uri
    app_id = sec.fyers_app_id.get_secret_value()
    vault = vault or open_vault(sec)
    code = auth.parse_redirect(pasted, state)
    token = auth.exchange_code(
        app_id, sec.fyers_app_secret.get_secret_value(), code, sec.fyers_redirect_uri
    )
    now = dt.datetime.now(dt.UTC)
    stored = auth.StoredToken(token, app_id, now, auth.token_expiry(now))
    vault.put(VAULT_KEY, stored.to_json())
    engine = make_engine(cfg.database.url, cfg.database.sqlite_busy_timeout_ms)
    with make_session_factory(engine)() as s:
        s.add(
            LoginSession(
                provider="fyers",
                purpose="data",
                instance_id=cfg.instance.instance_id,
                trading_date=now.astimezone(IST).date(),
                obtained_at=now,
                expires_at=stored.expires_at,
                status="VALID",
                token_fingerprint=hashlib.sha256(token.encode()).hexdigest()[-8:],
            )
        )
        s.commit()
    return stored.expires_at


def main() -> int:
    cfg = load_config()
    sec = Secrets()
    if not (sec.fyers_app_id and sec.fyers_app_secret and sec.fyers_redirect_uri):
        print(
            "Set ALGO_FYERS_APP_ID, ALGO_FYERS_APP_SECRET and ALGO_FYERS_REDIRECT_URI "
            "in .env first (see .env.example)."
        )
        return 2
    app_id = sec.fyers_app_id.get_secret_value()
    vault = open_vault(sec, confirm_new=True)
    state = auth.new_state()
    print("\n1) Open this link, log in to Fyers and finish 2FA:\n")
    print("   " + auth.login_url(app_id, sec.fyers_redirect_uri, state))
    print(
        "\n2) Paste the full address of the page Fyers sends you to (or the auth code that\n"
        "   page shows), then press Enter. Paste it ONLY here, never in a chat:"
    )
    pasted = input("> ")
    try:
        expires = finish_login(cfg, sec, pasted, state, vault)
    except auth.LoginError as e:
        print(f"Login failed: {e}")
        return 1
    print(
        f"\nLogged in. Token saved encrypted; valid until "
        f"{expires.astimezone(IST):%Y-%m-%d %H:%M} IST."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
