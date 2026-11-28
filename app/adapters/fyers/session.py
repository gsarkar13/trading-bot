"""Load today's Fyers token from the vault and build clients."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from app.adapters.fyers.auth import StoredToken, require_valid
from app.adapters.fyers.client import FyersClient
from app.config.schema import PlatformConfig
from app.config.secrets import Secrets
from app.core.ratelimit import RateLimiter
from app.core.vault import Vault, get_passphrase

VAULT_KEY = "fyers_data_token"


def open_vault(secrets: Secrets, confirm_new: bool = False) -> Vault:
    path = Path(secrets.vault_path)
    return Vault(path, get_passphrase(confirm_new=confirm_new and not path.exists()))


def load_token(vault: Vault, now: dt.datetime | None = None) -> StoredToken:
    raw = vault.get(VAULT_KEY)
    tok = StoredToken.from_json(raw) if raw else None
    return require_valid(tok, now or dt.datetime.now(dt.UTC))


def make_client(cfg: PlatformConfig, token: StoredToken) -> FyersClient:
    rl = RateLimiter(
        cfg.rate_limits.data_requests_per_second, cfg.rate_limits.data_requests_per_minute
    )
    return FyersClient(token.app_id, token.access_token, rl, Path("logs") / "fyers")
