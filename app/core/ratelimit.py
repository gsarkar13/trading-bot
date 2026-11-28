"""Token-bucket rate limiting (invariant 12). Per-second and per-minute buckets together."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    """Allows at most `per_second` calls in any 1 s window and `per_minute` in any 60 s.

    Sliding windows (exact) rather than refilling buckets, because Fyers blocks a user for
    the day after repeated per-minute breaches (BROKER_NOTES.md).
    """

    def __init__(
        self, per_second: float, per_minute: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.per_second = max(1, int(per_second))
        self.per_minute = per_minute
        self._clock = clock
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._tlock = threading.Lock()  # acquire_blocking may be called from several threads

    def wait_time(self) -> float:
        now = self._clock()
        while self._calls and now - self._calls[0] >= 60:
            self._calls.popleft()
        waits = [0.0]
        recent = [t for t in self._calls if now - t < 1]
        if len(recent) >= self.per_second:
            waits.append(1 - (now - recent[-self.per_second]))
        if len(self._calls) >= self.per_minute:
            waits.append(60 - (now - self._calls[-self.per_minute]))
        return max(waits)

    def recent(self) -> dict[str, int]:
        """For the dashboard's order-rate meter."""
        now = self._clock()
        return {
            "last_second": sum(1 for t in self._calls if now - t < 1),
            "last_minute": sum(1 for t in self._calls if now - t < 60),
            "per_second_limit": self.per_second,
            "per_minute_limit": self.per_minute,
        }

    def record(self) -> None:
        self._calls.append(self._clock())

    async def acquire(self) -> None:
        async with self._lock:
            while (w := self.wait_time()) > 0:
                await asyncio.sleep(w)
            self.record()

    def acquire_blocking(self) -> None:
        with self._tlock:
            while (w := self.wait_time()) > 0:
                time.sleep(w)
            self.record()
