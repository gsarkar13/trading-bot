"""One instance trades an account (invariant 16; failure mode F1).

* `InstanceLock`: an OS-level exclusive lock on a file next to the database. A second
  engine on the same machine (double-click, systemd plus a manual start) can't start.
* `foreign_orders`: orders in the broker's order book tagged by a DIFFERENT instance
  id. An execution instance checks at startup and every minute; any hit stops new
  entries and raises a critical alert.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import IO

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.adapters.base import BrokerOrder
from app.core.tags import is_foreign
from app.db.models import Instance


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - held for the process life
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise AlreadyRunning(
                f"another engine already holds {self.path}; stop it first "
                "(two engines must never run on one account)"
            ) from None
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self, t: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.release()


def foreign_orders(book: Sequence[BrokerOrder], my_instance_id: str) -> list[BrokerOrder]:
    return [o for o in book if is_foreign(o.client_tag, my_instance_id)]


def heartbeat(
    engine: Engine,
    instance_id: str,
    role: str,
    host: str,
    static_ip: str | None,
    ip_ok: bool | None,
    process_id: str,
    now: dt.datetime,
) -> None:
    with Session(engine) as s:
        row = s.get(Instance, instance_id)
        if row is None:
            row = Instance(
                instance_id=instance_id,
                role=role,
                host_label=host,
                registered_static_ip=static_ip,
                last_seen_at=now,
            )
            s.add(row)
        row.role, row.host_label, row.registered_static_ip = role, host, static_ip
        row.last_seen_at, row.last_ip_check_ok, row.process_start_id = now, ip_ok, process_id
        s.commit()
