"""Position sizing (brief section 9.1). ONE implementation, used by the live/paper engine
and by the backtester, so research and trading can never size differently.

lots = floor(risk_per_trade / (stop distance in premium x lot size)), then capped by
max_lots, the freeze quantity and the premium cap (max % of capital in premium). If not
even one lot fits, the trade is skipped and logged - risk is never raised to make it fit.
The broker-side backstop's worst-case loss must also stay within its allowed multiple
of the risk budget (proposal P3).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SizeResult:
    lots: int
    skip_reason: str | None
    by_risk: int
    by_cap: int


def size_lots(
    *,
    risk_inr: float,
    stop_dist: float,
    premium: float,
    lot: int,
    capital: float,
    max_premium_pct: float,
    max_lots: int,
    backstop_multiple: float,
    max_backstop_loss_multiple: float,
    freeze_qty: int | None = None,
) -> SizeResult:
    by_risk = math.floor(risk_inr / (stop_dist * lot)) if stop_dist > 0 and lot > 0 else 0
    cap = capital * max_premium_pct / 100
    by_cap = math.floor(cap / (premium * lot)) if premium > 0 and lot > 0 else 0
    lots = min(by_risk, by_cap, max_lots)
    if freeze_qty:
        lots = min(lots, freeze_qty // lot)
    if by_cap < 1:
        return SizeResult(0, "SKIPPED_SIZE_PREMIUM_CAP", by_risk, by_cap)
    if by_risk < 1:
        return SizeResult(0, "SKIPPED_SIZE_RISK", by_risk, by_cap)
    if lots < 1:
        return SizeResult(0, "SKIPPED_SIZE_FREEZE", by_risk, by_cap)
    backstop_loss = backstop_multiple * stop_dist * lot * lots
    if backstop_loss > max_backstop_loss_multiple * risk_inr + 1e-9:
        return SizeResult(0, "SKIPPED_BACKSTOP", by_risk, by_cap)
    return SizeResult(lots, None, by_risk, by_cap)
