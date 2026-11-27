"""Encrypted secret store (section 15, invariant 18).

Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from a master passphrase by scrypt
(n=2^15, r=8, p=1). The file holds only the salt and the ciphertext. Broker tokens live
here, never in `.env`, logs or the database (`login_sessions` keeps metadata only).

Passphrase source, in order: the `ALGO_VAULT_PASSPHRASE` environment variable (for an
unattended service; see RUNBOOK in Phase 3), else an interactive prompt.
"""

from __future__ import annotations

import base64
import contextlib
import getpass
import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

ENV_VAR = "ALGO_VAULT_PASSPHRASE"


class VaultError(Exception):
    pass


def _key(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def get_passphrase(confirm_new: bool = False) -> str:
    env = os.environ.get(ENV_VAR)
    if env:
        return env
    p = getpass.getpass("Vault passphrase: ")
    if confirm_new and getpass.getpass("Repeat passphrase: ") != p:
        raise VaultError("passphrases do not match")
    if len(p) < 8:
        raise VaultError("use a passphrase of at least 8 characters")
    return p


class Vault:
    def __init__(self, path: Path, passphrase: str) -> None:
        self.path = path
        self._pass = passphrase

    def _load(self) -> tuple[bytes, dict[str, Any]]:
        if not self.path.exists():
            return os.urandom(16), {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        salt = base64.b64decode(raw["salt"])
        try:
            data = Fernet(_key(self._pass, salt)).decrypt(raw["data"].encode("ascii"))
        except InvalidToken:
            raise VaultError("wrong passphrase or damaged vault file") from None
        return salt, json.loads(data)

    def get(self, name: str) -> Any:
        return self._load()[1].get(name)

    def put(self, name: str, value: Any) -> None:
        salt, data = self._load()
        data[name] = value
        token = Fernet(_key(self._pass, salt)).encrypt(json.dumps(data).encode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"v": 1, "salt": base64.b64encode(salt).decode(), "data": token.decode("ascii")}
            ),
            encoding="utf-8",
        )
        with contextlib.suppress(OSError):  # Windows: per-user profile permissions apply
            os.chmod(tmp, 0o600)
        tmp.replace(self.path)
