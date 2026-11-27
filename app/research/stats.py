"""Trade statistics with bootstrap confidence intervals (proposal P4)."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Stats:
    n: int
    win_rate: float | None
    expectancy: float | None
    ci90_low: float | None
    ci90_high: float | None
    profit_factor: float | None
    total: float
    max_drawdown: float  # positive number, in the same unit as the values

    def as_dict(self) -> dict[str, float | int | None]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def max_drawdown(values: Sequence[float]) -> float:
    peak = eq = dd = 0.0
    for v in values:
        eq += v
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return dd


def bootstrap_mean_ci(
    values: Sequence[float], samples: int, seed: int = 7, level: float = 0.90
) -> tuple[float, float] | tuple[None, None]:
    n = len(values)
    if n < 2:
        return None, None
    rnd = random.Random(seed)
    means = sorted(sum(rnd.choice(values) for _ in range(n)) / n for _ in range(samples))
    lo = means[int((1 - level) / 2 * samples)]
    hi = means[min(samples - 1, int((1 + level) / 2 * samples))]
    return lo, hi


def summarize(values: Sequence[float], samples: int = 2000) -> Stats:
    n = len(values)
    if n == 0:
        return Stats(0, None, None, None, None, None, 0.0, 0.0)
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    lo, hi = bootstrap_mean_ci(values, samples)
    pf = (sum(wins) / -sum(losses)) if losses else (math.inf if wins else None)
    return Stats(
        n,
        len(wins) / n,
        sum(values) / n,
        lo,
        hi,
        None if pf is None or math.isinf(pf) else pf,
        sum(values),
        max_drawdown(values),
    )


def diff_ci(
    a: Sequence[float], b: Sequence[float], samples: int = 2000, seed: int = 11
) -> tuple[float | None, float | None, float | None]:
    """Mean(a) - mean(b) with a 90% bootstrap CI (independent resampling)."""
    if len(a) < 2 or len(b) < 2:
        return None, None, None
    rnd = random.Random(seed)
    d = sorted(
        sum(rnd.choice(a) for _ in a) / len(a) - sum(rnd.choice(b) for _ in b) / len(b)
        for _ in range(samples)
    )
    return sum(a) / len(a) - sum(b) / len(b), d[int(0.05 * samples)], d[int(0.95 * samples)]


def verdict_vs_baseline(diff: float | None, lo: float | None) -> str:
    if diff is None:
        return "not enough trades to compare"
    if diff <= 0:
        return "no edge over baseline"
    if lo is None or lo <= 0:
        return "not distinguishable from baseline (confidence interval includes zero)"
    return "beats baseline"
