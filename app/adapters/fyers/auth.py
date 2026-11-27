"""Fyers daily login (section 12): login URL, redirect parsing, token exchange.

Verified against Fyers docs (BROKER_NOTES.md) and the fyers-apiv3 3.3.0 SDK source:
  * login:   GET  https://api-t1.fyers.in/api/v3/generate-authcode?client_id&redirect_uri
             &response_type=code&state
  * exchange: POST https://api-t1.fyers.in/api/v3/validate-authcode
             {grant_type: authorization_code, appIdHash: sha256("app_id:secret"), code}
  * the redirect carries `auth_code` (the token-exchange code) and may also carry
    `code=200` (an HTTP-style status). Only `auth_code` is ever used.
  * tokens expire at 06:30 IST the next day.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import urllib.parse
from dataclasses import dataclass
from typing import Any

from app.adapters.base import AuthExpiredError
from app.core.timeutil import IST

LOGIN_URL = "https://api-t1.fyers.in/api/v3/generate-authcode"
EXPIRY_TIME = dt.time(6, 30)


class LoginError(Exception):
    pass


def app_id_hash(app_id: str, secret: str) -> str:
    return hashlib.sha256(f"{app_id}:{secret}".encode()).hexdigest()


def new_state() -> str:
    return secrets.token_urlsafe(12)


def login_url(app_id: str, redirect_uri: str, state: str) -> str:
    q = urllib.parse.urlencode(
        {"client_id": app_id, "redirect_uri": redirect_uri, "response_type": "code", "state": state}
    )
    return f"{LOGIN_URL}?{q}"


def parse_redirect(url: str, expected_state: str | None) -> str:
    """Return the auth code from a pasted redirect URL (or its query string)."""
    url = url.strip()
    query = urllib.parse.urlparse(url).query if "?" in url or "://" in url else url
    params = urllib.parse.parse_qs(query)
    if params.get("s", ["ok"])[0].lower() != "ok":
        raise LoginError(f"Fyers reported an error: {params.get('message', ['?'])[0]}")
    codes = params.get("auth_code")
    if not codes or not codes[0]:
        raise LoginError("no auth_code in the URL (did you copy the full address?)")
    if expected_state is not None and params.get("state", [None])[0] != expected_state:
        raise LoginError("state does not match this login attempt; start the login again")
    return codes[0]


def token_expiry(obtained_at: dt.datetime) -> dt.datetime:
    """The next 06:30 IST after the token was obtained."""
    local = obtained_at.astimezone(IST)
    exp = dt.datetime.combine(local.date(), EXPIRY_TIME, IST)
    if local >= exp:
        exp += dt.timedelta(days=1)
    return exp.astimezone(dt.UTC)


@dataclass(frozen=True)
class StoredToken:
    access_token: str
    app_id: str
    obtained_at: dt.datetime
    expires_at: dt.datetime

    def valid_at(self, now: dt.datetime) -> bool:
        return now < self.expires_at

    def to_json(self) -> dict[str, str]:
        return {
            "access_token": self.access_token,
            "app_id": self.app_id,
            "obtained_at": self.obtained_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_json(cls, d: dict[str, str]) -> StoredToken:
        return cls(
            d["access_token"],
            d["app_id"],
            dt.datetime.fromisoformat(d["obtained_at"]),
            dt.datetime.fromisoformat(d["expires_at"]),
        )

    def __repr__(self) -> str:  # never print the token
        return f"StoredToken(app_id=..., expires_at={self.expires_at.isoformat()})"


def exchange_code(app_id: str, secret: str, auth_code: str, redirect_uri: str) -> str:
    """Exchange the auth code for an access token via the official SDK."""
    from fyers_apiv3 import fyersModel

    session = fyersModel.SessionModel(
        client_id=app_id,
        secret_key=secret,
        redirect_uri=redirect_uri,
        response_type="code",
        grant_type="authorization_code",
    )
    session.set_token(auth_code)
    resp: dict[str, Any] = session.generate_token()
    if resp.get("s") != "ok" or not resp.get("access_token"):
        raise LoginError(
            f"token exchange failed: code={resp.get('code')} message={resp.get('message')}"
        )
    token = str(resp["access_token"])
    return token


def require_valid(token: StoredToken | None, now: dt.datetime) -> StoredToken:
    if token is None:
        raise AuthExpiredError("not logged in to Fyers today; run: python -m app.tools.login")
    if not token.valid_at(now):
        raise AuthExpiredError("Fyers token expired (06:30 IST); run: python -m app.tools.login")
    return token
