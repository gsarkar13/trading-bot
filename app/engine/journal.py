"""End-of-day journal (brief section 15): one row per trading day with per-book stats.

The AI review (section 11, P2.5) is added to the same row later and is advisory only.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from decimal import Decimal

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.core.timeutil import IST
from app.db.models import EventLog, JournalEOD, SignalRow, Trade


def _bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    a = dt.datetime.combine(day, dt.time(0, 0), IST).astimezone(dt.UTC)
    return a, a + dt.timedelta(days=1)


def day_stats(engine: Engine, day: dt.date) -> dict[str, object]:
    a, b = _bounds(day)
    with Session(engine) as s:
        trades = list(s.scalars(select(Trade).where(Trade.exit_at >= a, Trade.exit_at < b)))
        signals = list(
            s.scalars(select(SignalRow).where(SignalRow.created_at >= a, SignalRow.created_at < b))
        )
        crit = list(
            s.scalars(
                select(EventLog).where(
                    EventLog.at >= a, EventLog.at < b, EventLog.severity == "critical"
                )
            )
        )
    books: dict[str, dict[str, object]] = {}
    by_book: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        by_book[t.book].append(t)
    for book, ts in sorted(by_book.items()):
        net = sum((t.net_pnl for t in ts), Decimal(0))
        wins = [t for t in ts if t.net_pnl > 0]
        books[book] = {
            "trades": len(ts),
            "wins": len(wins),
            "losses": len(ts) - len(wins),
            "net_pnl": str(net),
            "gross_pnl": str(sum((t.gross_pnl for t in ts), Decimal(0))),
            "charges": str(sum((t.charges_total for t in ts), Decimal(0))),
            "avg_compliance": (sum(t.compliance_score or 0 for t in ts) / len(ts)),
            "self_reported": sum(1 for t in ts if t.self_reported),
        }
    outcomes: Counter[str] = Counter()
    for sg in signals:
        outcomes[sg.outcome.split(":")[0].split(" by ")[0]] += 1
    return {
        "date": day.isoformat(),
        "books": books,
        "signals": len(signals),
        "signal_outcomes": dict(outcomes),
        "critical_events": [c.message for c in crit][:50],
    }


def write_journal(engine: Engine, day: dt.date, notes: str | None = None) -> dict[str, object]:
    stats = day_stats(engine, day)
    with Session(engine) as s:
        row = s.scalar(select(JournalEOD).where(JournalEOD.trading_date == day))
        if row is None:
            row = JournalEOD(trading_date=day, stats=stats)
            s.add(row)
        else:
            row.stats = stats
        if notes is not None:
            row.trader_notes = notes
        s.commit()
    return stats
