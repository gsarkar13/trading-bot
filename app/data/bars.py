"""Bar type used by indicators, strategies, backtests and the recorder.

Research and signal code uses floats (prices arrive from the broker with 2 decimals and
indicators are floating-point by nature). Money and order prices are converted to
Decimal at the order/ledger boundary (DECISIONS.md D27).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Bar:
    ts: dt.datetime  # candle OPEN time, UTC-aware
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    oi: int | None = None

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("Bar.ts must be timezone-aware")
        if not (self.low <= min(self.open, self.close) and self.high >= max(self.open, self.close)):
            raise ValueError(f"inconsistent OHLC at {self.ts}: {self}")

    @property
    def range(self) -> float:
        return self.high - self.low
