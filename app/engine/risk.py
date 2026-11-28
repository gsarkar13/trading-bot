"""Risk controls (brief sections 4, 9.1, 13): time guards, the fail-closed data gate and
persistent circuit breakers.

  * Invariant 13 - time guards (IST): no entries 09:15-09:30 or after 14:45; intraday
    positions are squared off at 15:10; delivery (CNC) positions are exempt.
  * Invariant 6 - fail closed: no NEW entries on stale ticks, a stale option chain, a
    candle gap, an unmapped instrument, an expired token, clock drift or unfinished
    warm-up. Open positions keep being managed.
  * Invariant 8 - circuit breakers: daily/weekly/monthly loss limits (realized + current
    unrealized), max trades per day and max consecutive losses. Stored in `risk_state`
    per book and period, so a restart can't clear them. A manual reset needs a typed
    phrase and is logged as a rule override.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.config.schema import FreshnessGuards, LossLimit, RiskConfig, TimeGuards
from app.core.timeutil import to_ist
from app.core.types import Product
from app.db.models import EventLog, RiskState

STREAK_START = dt.date(2000, 1, 1)  # the single "streak" row per book (Q7: spans days)


# ---------------------------------------------------------------- time guards (inv 13)
def entry_block_reason(now: dt.datetime, tg: TimeGuards) -> str | None:
    t = to_ist(now).time()
    if t < tg.session_open or t >= tg.session_close:
        return "market closed"
    if tg.no_entry_start <= t < tg.no_entry_end:
        return f"no entries {tg.no_entry_start:%H:%M}-{tg.no_entry_end:%H:%M}"
    if t > tg.last_entry:
        return f"no new entries after {tg.last_entry:%H:%M}"
    return None


def must_square_off(now: dt.datetime, tg: TimeGuards, product: Product) -> bool:
    return product is not Product.CNC and to_ist(now).time() >= tg.square_off


# ---------------------------------------------------------------- data gate (inv 6)
@dataclass(frozen=True)
class DataHealth:
    now: dt.datetime
    last_tick_at: dt.datetime | None
    chain_at: dt.datetime | None
    chain_needed: bool
    candle_gap: bool
    instruments_mapped: bool
    token_valid: bool
    clock_drift_ms: float | None
    warmed_up: bool


def data_block_reasons(h: DataHealth, g: FreshnessGuards, max_drift_ms: float) -> list[str]:
    r = []
    if h.last_tick_at is None or (h.now - h.last_tick_at).total_seconds() > g.max_tick_age_s:
        r.append("stale ticks")
    if h.chain_needed and (
        h.chain_at is None or (h.now - h.chain_at).total_seconds() > g.max_chain_age_s
    ):
        r.append("stale option chain")
    if h.candle_gap:
        r.append("gap in candles")
    if not h.instruments_mapped:
        r.append("instrument not mapped")
    if not h.token_valid:
        r.append("token expired")
    if h.clock_drift_ms is None or abs(h.clock_drift_ms) > max_drift_ms:
        r.append("clock drift unknown or too large")
    if not h.warmed_up:
        r.append("indicators warming up")
    return r


# ---------------------------------------------------------------- breakers (inv 8)
def _limit(ll: LossLimit, capital: Decimal) -> Decimal:
    v = capital * Decimal(str(ll.pct_of_capital)) / 100
    return min(v, ll.inr) if ll.inr is not None else v


def period_starts(d: dt.date) -> dict[str, dt.date]:
    return {"day": d, "week": d - dt.timedelta(days=d.weekday()), "month": d.replace(day=1)}


@dataclass
class BreakerStatus:
    tripped: bool
    reasons: list[str] = field(default_factory=list)


class Breakers:
    """Circuit breakers for one book (e.g. "REAL" or "PAPER:pdh_pdl_breakout")."""

    def __init__(self, engine: Engine, cfg: RiskConfig, book: str, instance_id: str) -> None:
        self.engine, self.cfg, self.book, self.instance_id = engine, cfg, book, instance_id

    def _row(self, s: Session, period: str, start: dt.date, now: dt.datetime) -> RiskState:
        row = s.scalar(
            select(RiskState).where(
                RiskState.book == self.book,
                RiskState.period == period,
                RiskState.period_start == start,
            )
        )
        if row is None:
            row = RiskState(
                book=self.book,
                period=period,
                period_start=start,
                realized_pnl=Decimal(0),
                worst_unrealized_pnl=Decimal(0),
                trades=0,
                consecutive_losses=0,
                tripped=False,
                updated_at=now,
            )
            s.add(row)
            s.flush()
        return row

    def _rows(self, s: Session, now: dt.datetime) -> dict[str, RiskState]:
        d = to_ist(now).date()
        rows = {p: self._row(s, p, start, now) for p, start in period_starts(d).items()}
        rows["streak"] = self._row(s, "streak", STREAK_START, now)
        return rows

    def record_entry(self, now: dt.datetime) -> None:
        with Session(self.engine) as s:
            r = self._rows(s, now)
            r["day"].trades += 1
            r["day"].updated_at = now
            s.commit()

    def record_close(self, now: dt.datetime, net_pnl: Decimal) -> None:
        with Session(self.engine) as s:
            r = self._rows(s, now)
            for p in ("day", "week", "month"):
                r[p].realized_pnl += net_pnl
                r[p].updated_at = now
            st = r["streak"]
            st.consecutive_losses = st.consecutive_losses + 1 if net_pnl < 0 else 0
            st.updated_at = now
            s.commit()

    def check(self, now: dt.datetime, unrealized: Decimal = Decimal(0)) -> BreakerStatus:
        """Evaluate and persist. Once tripped, a period stays tripped until it ends (or a
        logged manual reset); the streak stays tripped until a reset (Q7)."""
        cap = self.cfg.capital_inr
        limits = {
            "day": _limit(self.cfg.daily_loss, cap),
            "week": _limit(self.cfg.weekly_loss, cap),
            "month": _limit(self.cfg.monthly_loss, cap),
        }
        reasons: list[str] = []
        with Session(self.engine) as s:
            r = self._rows(s, now)
            for p, lim in limits.items():
                row = r[p]
                total = row.realized_pnl + unrealized
                row.worst_unrealized_pnl = min(row.worst_unrealized_pnl, unrealized)
                if not row.tripped and total <= -lim:
                    self._trip(s, row, now, f"{p} loss {total:.0f} <= -{lim:.0f}")
            if not r["day"].tripped and r["day"].trades >= self.cfg.max_trades_per_day:
                self._trip(s, r["day"], now, f"max {self.cfg.max_trades_per_day} trades/day")
            st = r["streak"]
            if not st.tripped and st.consecutive_losses >= self.cfg.max_consecutive_losses:
                self._trip(s, st, now, f"{st.consecutive_losses} consecutive losses")
            for row in r.values():
                if row.tripped and row.tripped_reason:
                    reasons.append(row.tripped_reason)
            s.commit()
        return BreakerStatus(bool(reasons), reasons)

    def _trip(self, s: Session, row: RiskState, now: dt.datetime, why: str) -> None:
        row.tripped, row.tripped_reason, row.tripped_at, row.updated_at = True, why, now, now
        s.add(
            EventLog(
                at=now,
                category="RISK",
                severity="critical",
                instance_id=self.instance_id,
                message=f"{self.book}: breaker: {why}",
                data={"book": self.book, "period": row.period},
            )
        )

    @staticmethod
    def reset_phrase(now: dt.datetime) -> str:
        return f"RESET BREAKERS {to_ist(now):%Y-%m-%d}"

    def reset(self, now: dt.datetime, typed: str, by: str) -> bool:
        """Manual reset: needs the exact phrase; logged as a rule override."""
        if typed.strip() != self.reset_phrase(now):
            return False
        with Session(self.engine) as s:
            for row in self._rows(s, now).values():
                if row.tripped:
                    row.tripped, row.reset_override_at = False, now
                    row.reset_override_by, row.reset_override_reason = by, typed
                    if row.period == "streak":
                        row.consecutive_losses = 0
                    if row.period == "day" and row.trades >= self.cfg.max_trades_per_day:
                        row.trades = self.cfg.max_trades_per_day - 1
            s.add(
                EventLog(
                    at=now,
                    category="RISK",
                    severity="critical",
                    instance_id=self.instance_id,
                    message=f"{self.book}: RULE OVERRIDE: breakers reset by {by}",
                    data={"book": self.book},
                )
            )
            s.commit()
        return True
