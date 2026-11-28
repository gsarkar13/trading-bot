import datetime as dt

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.adapters.base import RateLimitedError
from app.data.bars import Bar
from app.db.models import BackfillChunk
from app.tools.backfill import default_series, market_hours, run_backfill
from tests.helpers import session_bars, trading_days

START, END = dt.date(2026, 6, 1), dt.date(2026, 10, 30)


def fake_fetch(calls: list[tuple[str, str]], fail_first: int = 0):  # type: ignore[no-untyped-def]
    state = {"fails": fail_first}

    def fetch(sym: str, res: str, a: dt.date, b: dt.date, cont: bool) -> list[Bar]:
        if state["fails"]:
            state["fails"] -= 1
            raise RateLimitedError("slow down")
        calls.append((sym, res))
        days = [d for d in trading_days(a, 200) if d <= b][:5]
        if res == "D":
            return [session_bars(d, 100, i, n=1)[0] for i, d in enumerate(days)]
        return [x for i, d in enumerate(days) for x in session_bars(d, 24000, i, n=3)]

    return fetch


def test_backfill_resumes_and_retries(migrated_engine: Engine) -> None:
    series = default_series({"NIFTY": "NSE:NIFTY26DECFUT"})
    calls: list[tuple[str, str]] = []
    sleeps: list[float] = []
    s1 = run_backfill(
        migrated_engine,
        fake_fetch(calls, fail_first=1),
        series,
        START,
        END,
        today=dt.date(2026, 11, 27),
        log=lambda _: None,
        sleep=sleeps.append,
    )
    n_first = len(calls)
    assert sleeps == [15] and n_first == 4 * (2 + 1)  # 4 series x (2 intraday + 1 daily chunks)
    assert s1["series"]["INDEX|NIFTY 1m"]["total_bars"] > 0
    calls.clear()
    s2 = run_backfill(
        migrated_engine,
        fake_fetch(calls),
        series,
        START,
        END,
        today=dt.date(2026, 11, 27),
        log=lambda _: None,
    )
    assert calls == []  # everything resumed from backfill_chunks
    assert s2["series"]["FUTC|NIFTY 1m"]["skipped"] == 2


def test_chunk_containing_today_is_not_marked_done(migrated_engine: Engine) -> None:
    series = default_series({})[:1]
    calls: list[tuple[str, str]] = []
    run_backfill(
        migrated_engine,
        fake_fetch(calls),
        series,
        dt.date(2026, 11, 20),
        dt.date(2026, 11, 27),
        today=dt.date(2026, 11, 27),
        log=lambda _: None,
    )
    with Session(migrated_engine) as s:
        assert s.scalar(select(func.count(BackfillChunk.id))) == 0
    run_backfill(
        migrated_engine,
        fake_fetch(calls),
        series,
        dt.date(2026, 11, 20),
        dt.date(2026, 11, 27),
        today=dt.date(2026, 11, 27),
        log=lambda _: None,
    )
    assert len(calls) == 4  # fetched again the second time


@pytest.mark.parametrize(
    "h, m, wd, expect", [(10, 0, 0, True), (8, 59, 0, False), (15, 46, 0, False), (10, 0, 5, False)]
)
def test_market_hours_guard(h: int, m: int, wd: int, expect: bool) -> None:
    from app.core.timeutil import IST

    d = dt.date(2026, 11, 30) + dt.timedelta(days=wd)  # Monday + wd
    assert market_hours(dt.datetime(d.year, d.month, d.day, h, m, tzinfo=IST)) is expect


def test_refused_chunk_is_recorded_and_the_rest_continues(migrated_engine: Engine) -> None:
    from app.adapters.base import AdapterError

    calls: list[tuple[str, str]] = []
    ok = fake_fetch(calls)

    def fetch(sym: str, res: str, a: dt.date, b: dt.date, cont: bool) -> list[Bar]:
        if a < dt.date(2026, 7, 1) and res == "1":
            raise AdapterError("Fyers error -50: range beyond plan limit")
        return ok(sym, res, a, b, cont)

    s = run_backfill(
        migrated_engine,
        fetch,
        default_series({})[:1],
        START,
        END,
        today=dt.date(2026, 11, 27),
        log=lambda _: None,
    )
    info = s["series"]["INDEX|NIFTY 1m"]
    assert len(info["errors"]) == 1 and "plan limit" in info["errors"][0]
    assert info["fetched"] > 0  # the later chunk still came in
