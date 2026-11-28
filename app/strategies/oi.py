"""OI pulse from option-chain snapshots (section 7.4).

Over the last W minutes, at ATM +/- k strikes: the net change in put OI and call OI, and
PCR (by OI, over every strike in the snapshot) at the start and end of the window.
Returns None (UNKNOWN) when the newest snapshot is older than `max_age_s`, when there is
no snapshot near the start of the window, or when strikes are missing (invariant 6).
"""

from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import dataclass

from app.strategies.base import OIPulse


@dataclass(frozen=True)
class ChainPoint:
    taken_at: dt.datetime
    spot: float
    put_oi: dict[float, int]  # strike -> OI
    call_oi: dict[float, int]

    @property
    def pcr(self) -> float:
        c = sum(self.call_oi.values())
        return sum(self.put_oi.values()) / c if c > 0 else float("nan")


class OIPulseTracker:
    def __init__(self, window_min: float, k: int, max_age_s: float) -> None:
        self.window = dt.timedelta(minutes=window_min)
        self.k, self.max_age_s = k, max_age_s
        self._points: list[ChainPoint] = []
        self._times: list[dt.datetime] = []

    def add(self, p: ChainPoint) -> None:
        if self._times and p.taken_at <= self._times[-1]:
            return  # out of order or duplicate snapshot: ignore
        self._points.append(p)
        self._times.append(p.taken_at)

    def pulse_at(self, now: dt.datetime) -> OIPulse | None:
        i = bisect.bisect_right(self._times, now) - 1
        if i < 0:
            return None
        end = self._points[i]
        age = (now - end.taken_at).total_seconds()
        if age > self.max_age_s:
            return None
        j = bisect.bisect_left(self._times, now - self.window)
        if j >= i:
            return None
        start = self._points[j]
        # the start snapshot must actually be near the window start (not a late gap fill)
        if (start.taken_at - (now - self.window)).total_seconds() > self.max_age_s:
            return None
        strikes = sorted(set(end.put_oi) & set(end.call_oi))
        if not strikes:
            return None
        atm = min(strikes, key=lambda s: (abs(s - end.spot), s))
        a = strikes.index(atm)
        band = strikes[max(0, a - self.k) : a + self.k + 1]
        if len(band) < 2 * self.k + 1 or any(
            s not in start.put_oi or s not in start.call_oi for s in band
        ):
            return None
        put_chg = sum(end.put_oi[s] - start.put_oi[s] for s in band)
        call_chg = sum(end.call_oi[s] - start.call_oi[s] for s in band)
        return OIPulse(
            window_min=self.window.total_seconds() / 60,
            put_oi_change=put_chg,
            call_oi_change=call_chg,
            pcr_start=start.pcr,
            pcr_end=end.pcr,
            age_s=age,
        )
