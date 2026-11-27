"""Client order tags (invariant 5).

Format: <instance_id><YYMMDD><seq:06d>, letters and digits only, at most 16 chars.
  * Fyers orderTag allows 1-30 letters/digits (checked 2026-11-27, BROKER_NOTES.md).
  * Flattrade's tag field and its limit are UNVERIFIED; 16 alphanumerics is a safe guess.
The tag is stored in the database *before* the order is sent; after any timeout the
order book is searched for it before a retry.
"""

from __future__ import annotations

import datetime as dt
import re

TAG_RE = re.compile(r"^(?P<inst>[A-Z0-9]{2,4})(?P<date>\d{6})(?P<seq>\d{6})$")


def make_tag(instance_id: str, trading_date: dt.date, seq: int) -> str:
    if not re.fullmatch(r"[A-Z0-9]{2,4}", instance_id):
        raise ValueError("instance_id must be 2-4 upper-case letters/digits")
    if not 2000 <= trading_date.year <= 2099:
        raise ValueError("trading_date must be in 2000-2099")
    if not 0 <= seq <= 999_999:
        raise ValueError("seq out of range")
    return f"{instance_id}{trading_date:%y%m%d}{seq:06d}"


def parse_tag(tag: str) -> tuple[str, dt.date, int] | None:
    """Return (instance_id, date, seq), or None for an untagged / foreign-format order.

    The instance id has variable length (2-4), so try each split and accept only one
    that yields a valid date.
    """
    if not re.fullmatch(r"[A-Z0-9]{14,16}", tag):
        return None
    inst_len = len(tag) - 12
    inst, date_s, seq_s = tag[:inst_len], tag[inst_len : inst_len + 6], tag[inst_len + 6 :]
    if not date_s.isdigit() or not seq_s.isdigit():
        return None
    try:  # explicit 20YY: strptime's %y would read 69-99 as 1969-1999
        d = dt.date(2000 + int(date_s[:2]), int(date_s[2:4]), int(date_s[4:]))
    except ValueError:
        return None
    return inst, d, int(seq_s)


def is_foreign(tag: str | None, my_instance_id: str) -> bool:
    """Invariant 16: an order tagged in our format by a *different* instance."""
    if not tag:
        return False
    parsed = parse_tag(tag)
    return parsed is not None and parsed[0] != my_instance_id
