"""Set the dashboard password: `python -m app.tools.set_password`.

Asks twice (input hidden), then writes only the scrypt HASH to `.env` as
ALGO_DASHBOARD_PASSWORD_HASH. The password itself is never stored or printed.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

from app.api.auth import hash_password

KEY = "ALGO_DASHBOARD_PASSWORD_HASH"


def write_hash(env: Path, h: str) -> None:
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    lines = [ln for ln in lines if not ln.startswith(f"{KEY}=")] + [f"{KEY}={h}"]
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    a = getpass.getpass("New dashboard password (min 10 characters): ")
    if len(a) < 10:
        print("Too short.")
        return 1
    if getpass.getpass("Again: ") != a:
        print("The two entries differ.")
        return 1
    write_hash(Path(".env"), hash_password(a))
    print("Saved (hash only) in .env. Restart the engine to use it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
