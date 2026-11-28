"""Dashboard login (section 15 "Access").

* Password stored only as a scrypt hash (`ALGO_DASHBOARD_PASSWORD_HASH`), set with
  `python -m app.tools.set_password`.
* Sessions live in memory: a restart logs everyone out (like arming).
* Real-money actions need a *fresh* login (`REAUTH_S`), per the v1 rule.
* The watchdog / Telegram process authenticates with a random token written to a file
  readable only by this user, and only from 127.0.0.1.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

SESSION_S = 12 * 3600
REAUTH_S = 300
N, R, P = 2**14, 8, 1


def hash_password(pw: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=N, r=R, p=P, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        algo, salt_hex, h_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex), n=N, r=R, p=P, dklen=32)
    return hmac.compare_digest(h.hex(), h_hex)


@dataclass
class Session:
    created: float
    login_at: float


@dataclass
class Auth:
    password_hash: str | None
    token_path: Path | None = None
    sessions: dict[str, Session] = field(default_factory=dict)
    failures: list[float] = field(default_factory=list)
    local_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    def __post_init__(self) -> None:
        if self.token_path is not None:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(self.local_token, encoding="utf-8")
            with contextlib.suppress(OSError):  # Windows: the file is in the user's folder
                os.chmod(self.token_path, 0o600)

    @property
    def required(self) -> bool:
        return self.password_hash is not None

    def login(self, pw: str) -> str | None:
        now = time.monotonic()
        self.failures = [t for t in self.failures if now - t < 300]
        if len(self.failures) >= 5:
            return None  # five wrong passwords: locked for five minutes
        if self.password_hash is None or not check_password(pw, self.password_hash):
            self.failures.append(now)
            return None
        sid = secrets.token_urlsafe(32)
        self.sessions[sid] = Session(now, now)
        return sid

    def valid(self, sid: str | None) -> Session | None:
        if not self.required:
            return Session(0, 0)
        s = self.sessions.get(sid or "")
        if s is None or time.monotonic() - s.created > SESSION_S:
            return None
        return s

    def fresh(self, sid: str | None) -> bool:
        """For real-money actions: a password login within the last few minutes."""
        if not self.required:
            return False  # no password set: real money can't be armed from the dashboard
        s = self.sessions.get(sid or "")
        return s is not None and time.monotonic() - s.login_at <= REAUTH_S

    def local_ok(self, token: str | None, host: str | None) -> bool:
        return (
            host in ("127.0.0.1", "::1")
            and token is not None
            and hmac.compare_digest(token, self.local_token)
        )

    def logout(self, sid: str | None) -> None:
        self.sessions.pop(sid or "", None)
