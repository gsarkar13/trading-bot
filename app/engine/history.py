"""History for the live engine: warm-up candles, prior-day candle, OI pulse, chain age.

Warm-up views are built with the backtester's own `build_views` from the same stored
candles, so a plugin sees identical inputs in backtest, replay and live (section 14.1).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from sqlalchemy import Engine, text

from app.config.loader import REPO_ROOT
from app.config.schema import Ema200HammerParams, PlatformConfig
from app.core.timeutil import to_ist
from app.data.bars import Bar
from app.research.backtest import build_views, roll_reset_days
from app.research.contracts import ExpiryCalendar, load_specs
from app.research.dataset import Dataset, load_chain_points, load_dataset
from app.strategies.base import MarketView, OIPulse
from app.strategies.oi import OIPulseTracker


def _s1(cfg: PlatformConfig) -> Ema200HammerParams:
    """Strategy 1's data settings (OI window, roll days); defaults if not configured."""
    if not any(s.id == "ema200_hammer_rvol_oi" for s in cfg.strategies):
        return Ema200HammerParams()
    p = cfg.strategy_params("ema200_hammer_rvol_oi")
    assert isinstance(p, Ema200HammerParams)
    return p


def warmup_views(
    cfg: PlatformConfig, engine: Engine, underlyings: Iterable[str], today: dt.date, days: int
) -> tuple[dict[str, list[MarketView]], Dataset]:
    """Every stored 1-minute view of the last `days` calendar days before today."""
    a = today - dt.timedelta(days=days)
    b = today - dt.timedelta(days=1)
    ds = load_dataset(
        engine,
        tuple(underlyings),
        a,
        b,
        with_options=False,
        on_expiry_day=cfg.contracts.nifty_expiry_day,
    )
    specs = load_specs(REPO_ROOT / "config" / "contracts.yaml")
    s1 = _s1(cfg)
    out: dict[str, list[MarketView]] = {}
    for u, d in ds.per_underlying.items():
        if not d.bars:
            out[u] = []
            continue
        cal = ExpiryCalendar(specs[u], d.trading_days)
        out[u] = [
            v
            for v, _ in build_views(
                d,
                cal,
                s1.oi_window_min,
                s1.oi_strikes_each_side,
                cfg.freshness.max_chain_age_s,
                s1.futures_roll_days_before_expiry,
            )
        ]
    return out, ds


class DbHistory:
    """Implements the engine's `History` protocol from the database."""

    def __init__(
        self,
        cfg: PlatformConfig,
        engine: Engine,
        ds: Dataset | None = None,
        today: dt.date | None = None,
    ) -> None:
        self.cfg, self.engine, self.ds = cfg, engine, ds
        s1 = _s1(cfg)
        self.s1 = s1
        self._trackers: dict[str, OIPulseTracker] = {}
        self._seen: dict[str, set[str]] = {}
        self._reset: dict[str, set[dt.date]] = {}
        if ds is not None and today is not None:
            specs = load_specs(REPO_ROOT / "config" / "contracts.yaml")
            for u, d in ds.per_underlying.items():
                days = [*d.trading_days, today]
                self._reset[u] = roll_reset_days(
                    days, ExpiryCalendar(specs[u], days), s1.futures_roll_days_before_expiry
                )

    def prior_day(self, underlying: str, d: dt.date) -> Bar | None:
        if self.ds is not None and underlying in self.ds.per_underlying:
            b = self.ds.per_underlying[underlying].prior_day(d)
            if b is not None:
                return b
        return None

    def roll_reset(self, underlying: str, d: dt.date) -> bool:
        return d in self._reset.get(underlying, set())

    def _tracker(self, u: str) -> OIPulseTracker:
        t = self._trackers.get(u)
        if t is None:
            t = OIPulseTracker(
                self.s1.oi_window_min,
                self.s1.oi_strikes_each_side,
                self.cfg.freshness.max_chain_age_s,
            )
            self._trackers[u] = t
            self._seen[u] = set()
        return t

    def oi_pulse(self, underlying: str, now: dt.datetime) -> OIPulse | None:
        t = self._tracker(underlying)
        d = to_ist(now).date()
        for p in load_chain_points(
            self.engine, underlying, d, d, self.cfg.contracts.nifty_expiry_day
        ):
            sid = p.taken_at.isoformat()
            if sid not in self._seen[underlying]:
                self._seen[underlying].add(sid)
                t.add(p)
        return t.pulse_at(now)

    def chain_at(self, underlying: str) -> dt.datetime | None:
        with self.engine.connect() as c:
            v = c.execute(
                text("SELECT MAX(taken_at) FROM option_chain_snapshots WHERE underlying = :u"),
                {"u": underlying},
            ).scalar()
        if v is None:
            return None
        ts = v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v))
        return ts if ts.tzinfo else ts.replace(tzinfo=dt.UTC)
