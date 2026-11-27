"""Phase 0 sizing-feasibility estimate (brief section 9.1).

Answers: at a given risk per trade, what share of plausible signals can 1 lot take?

This is an ESTIMATE from assumed inputs, not from data. Phase 1 replaces it with the
measured share from backtests (section 16.4, "sizing feasibility").

Usage (rerun with real values from the instrument master and a live option chain):
    python -m app.tools.sizing_estimate
    python -m app.tools.sizing_estimate --nifty-lot 65 --nifty-premium 130 \\
        --bn-lot 30 --bn-premium 550
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass

CAPITAL = 50_000
MAX_PREMIUM_PCT = 40


@dataclass(frozen=True)
class Underlying:
    name: str
    lot: int
    atm_premium: float
    delta: float
    slippage_per_unit: float  # spread + re-price cost, premium points, both sides
    stop_points: tuple[int, ...]  # plausible strategy-stop distances on the index


def lots_for(u: Underlying, stop_pts: float, risk: float) -> tuple[int, float, str]:
    """Return (lots, rupee risk of 1 lot, reason)."""
    per_unit = stop_pts * u.delta + u.slippage_per_unit
    one_lot_risk = per_unit * u.lot
    by_risk = math.floor(risk / one_lot_risk)
    by_premium = math.floor(CAPITAL * MAX_PREMIUM_PCT / 100 / (u.atm_premium * u.lot))
    lots = min(by_risk, by_premium, 1)  # max_lots = 1
    reason = "ok" if lots >= 1 else ("premium cap" if by_premium < 1 else "risk budget")
    return lots, one_lot_risk, reason


def table(us: list[Underlying], risks: list[int]) -> str:
    lines = []
    for u in us:
        lines.append(
            f"\n{u.name}: lot {u.lot}, ATM premium ~Rs {u.atm_premium:g}, delta {u.delta}, "
            f"1 lot costs Rs {u.lot * u.atm_premium:,.0f} "
            f"(cap Rs {CAPITAL * MAX_PREMIUM_PCT / 100:,.0f})"
        )
        lines.append(
            "| stop (index pts) | 1-lot risk Rs | " + " | ".join(f"Rs {r}" for r in risks) + " |"
        )
        lines.append("|---|---|" + "---|" * len(risks))
        takes = dict.fromkeys(risks, 0)
        for s in u.stop_points:
            cells = []
            for r in risks:
                lots, one, why = lots_for(u, s, r)
                cells.append("take" if lots else f"skip ({why})")
                takes[r] += lots >= 1
            lines.append(f"| {s} | {one:,.0f} | " + " | ".join(cells) + " |")
        n = len(u.stop_points)
        lines.append(
            "| **share taken** | | "
            + " | ".join(f"**{takes[r]}/{n} = {100 * takes[r] / n:.0f}%**" for r in risks)
            + " |"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--nifty-lot", type=int, default=65)
    ap.add_argument("--nifty-premium", type=float, default=120)
    ap.add_argument("--bn-lot", type=int, default=30)
    ap.add_argument("--bn-premium", type=float, default=500)
    ap.add_argument("--risks", type=int, nargs="+", default=[500, 1000, 1500])
    a = ap.parse_args()
    us = [
        # Stops: 1-minute hammer/breakout-candle stops. NIFTY 1m ATR is roughly 8-20 pts,
        # so stop = candle range + buffer is plausibly 10-60 pts. BANKNIFTY is ~2.2x NIFTY.
        Underlying(
            "NIFTY (weekly)",
            a.nifty_lot,
            a.nifty_premium,
            0.5,
            1.5,
            (10, 15, 20, 25, 30, 40, 50, 60),
        ),
        Underlying(
            "BANKNIFTY (monthly)",
            a.bn_lot,
            a.bn_premium,
            0.5,
            4.0,
            (25, 35, 45, 55, 70, 90, 110, 130),
        ),
    ]
    print(table(us, a.risks))


if __name__ == "__main__":
    main()
