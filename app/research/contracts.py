"""Contract specs and expiry calendar for modeled backtests (config/contracts.yaml).

Expiry rule: weekly expiries fall on the configured weekday; the monthly expiry is the
last such weekday of the month. If that day is not a trading day, expiry moves to the
previous trading day (brief 3.2). Trading days come from the downloaded data (D14); for
dates after the data ends, weekdays are assumed open.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Spec:
    underlying: str
    strike_interval: float
    weekly: bool
    lots: tuple[tuple[dt.date, int, bool], ...]
    weekdays: tuple[tuple[dt.date, int, bool], ...]

    def lot_size(self, d: dt.date) -> tuple[int, bool]:
        cur = self.lots[0]
        for row in self.lots:
            if row[0] <= d:
                cur = row
        return cur[1], cur[2]

    def weekday(self, d: dt.date) -> int:
        cur = self.weekdays[0][1]
        for frm, wd, _ in self.weekdays:
            if frm <= d:
                cur = wd
        return cur


def load_specs(path: Path) -> dict[str, Spec]:
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = {}
    for u, v in raw.items():
        if not isinstance(v, dict):
            continue
        out[u] = Spec(
            underlying=u,
            strike_interval=float(v["strike_interval"]),
            weekly=bool(v["weekly_options"]),
            lots=tuple(
                sorted(
                    (
                        dt.date.fromisoformat(x["from"]),
                        int(x["lot"]),
                        bool(x.get("verified", False)),
                    )
                    for x in v["lot_sizes"]
                )
            ),
            weekdays=tuple(
                sorted(
                    (
                        dt.date.fromisoformat(x["from"]),
                        int(x["weekday"]),
                        bool(x.get("verified", False)),
                    )
                    for x in v["expiry_weekday"]
                )
            ),
        )
    return out


class ExpiryCalendar:
    def __init__(self, spec: Spec, trading_days: Collection[dt.date]) -> None:
        self.spec = spec
        self.days = set(trading_days)
        self.last_data_day = max(self.days) if self.days else dt.date.min

    def _open(self, d: dt.date) -> bool:
        if d > self.last_data_day:
            return d.weekday() < 5
        return d in self.days

    def _shift_back(self, d: dt.date) -> dt.date:
        while not self._open(d):
            d -= dt.timedelta(days=1)
        return d

    def monthly(self, year: int, month: int) -> dt.date:
        last = dt.date(year, month, calendar.monthrange(year, month)[1])
        wd = self.spec.weekday(last)
        d = last - dt.timedelta(days=(last.weekday() - wd) % 7)
        return self._shift_back(d)

    def next_monthly(self, d: dt.date) -> dt.date:
        e = self.monthly(d.year, d.month)
        if e >= d:
            return e
        y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        return self.monthly(y, m)

    def next_weekly(self, d: dt.date) -> dt.date:
        wd = self.spec.weekday(d)
        cand = d + dt.timedelta(days=(wd - d.weekday()) % 7)
        e = self._shift_back(cand)
        if e < d:  # shifted before today: use the following week's
            e = self._shift_back(cand + dt.timedelta(days=7))
        return e

    def trading_expiry(self, d: dt.date, on_expiry_day: str) -> dt.date:
        """Expiry to trade on date d: NIFTY nearest weekly, BANKNIFTY current monthly.
        On the expiry day itself, `on_expiry_day` = "next_weekly" moves to the next one
        (D24); "skip" is handled by the caller."""
        e = self.next_weekly(d) if self.spec.weekly else self.next_monthly(d)
        if e == d and on_expiry_day == "next_weekly":
            e = (
                self.next_weekly(d + dt.timedelta(days=1))
                if self.spec.weekly
                else self.next_monthly(d + dt.timedelta(days=1))
            )
        return e

    def monthly_expiries_between(self, a: dt.date, b: dt.date) -> list[dt.date]:
        out, y, m = [], a.year, a.month
        while dt.date(y, m, 1) <= b:
            e = self.monthly(y, m)
            if a <= e <= b:
                out.append(e)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out
