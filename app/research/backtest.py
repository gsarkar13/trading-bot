"""Backtest engine (section 16.4).

One run = several *books* over the same data. Each book has its own plugin instance and
its own position limits, and takes every signal its filter accepts (unconstrained books,
proposal P1), so the baseline's record never depends on what another strategy did.

Fill rules (stated in every report):
  * A signal on a candle's close enters at the NEXT candle's open (index price).
  * Long-CE trades are long the index; long-PE trades are short the index (in points).
  * In a candle, a stop is checked before the target (conservative). If the candle opens
    beyond the stop, the exit is at the open (gap); else at the stop level.
  * Entries only between 09:30 and 14:45 IST; open trades are squared off at the open of
    the 15:10 candle. One open trade per underlying; max concurrent trades, max trades per
    day and the cooldown come from config.risk.
  * No entry right after a missing minute, or without a same-minute futures candle
    (invariant 6: fail closed).

Result layers:
  1. underlying: R multiple, MFE/MAE, "2R before 1R" first touch, by time and weekday
  2. MODELED option P&L: Black-Scholes repricing with IV from India VIX (scaled), real
     expiry calendar, spread + slippage, dated charges and lot sizes; IV sensitivity
  3. REAL option P&L: only where recorded option candles exist
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.config.schema import PlatformConfig
from app.core.timeutil import IST, to_ist
from app.data.bars import Bar
from app.research.contracts import ExpiryCalendar, Spec
from app.research.costs import ChargesTable
from app.research.dataset import Dataset, UnderlyingData
from app.research.options import bs_price, pick_strike, years_to_expiry
from app.strategies.base import MarketView, OpenPosition, Signal, StrategyPlugin
from app.strategies.oi import OIPulseTracker

M = dt.timedelta(minutes=1)


@dataclass
class BookSpec:
    name: str
    strategy_id: str
    make_plugin: Callable[[], StrategyPlugin]
    accept: Callable[[Signal], bool] = lambda s: True
    description: str = ""


@dataclass
class Leg:
    """A priced option leg for one IV scale (layer 2) or from real candles (layer 3)."""

    strike: float
    expiry: dt.date
    option_type: str
    lot_size: int
    entry_premium: float
    exit_premium: float
    iv: float | None
    lots_sized: int
    skip_reason: str | None
    net_1lot: float
    charges_1lot: float
    net_sized: float
    charges_sized: float


@dataclass
class Trade:
    book: str
    strategy_id: str
    underlying: str
    direction: str
    signal_at: dt.datetime
    entry_at: dt.datetime
    entry: float
    stop: float
    target: float | None
    risk_pts: float
    checklist: dict[str, Any]
    config_hash: str
    exit_at: dt.datetime | None = None
    exit: float | None = None
    exit_reason: str = ""
    mfe_pts: float = 0.0
    mae_pts: float = 0.0
    first_touch: str = "none"  # "2R", "stop" or "none" (by 15:10)
    fwd_mfe_r: float = 0.0
    fwd_mae_r: float = 0.0
    modeled: dict[float, Leg] = field(default_factory=dict)
    real: Leg | None = None

    @property
    def long(self) -> bool:
        return self.direction in ("LONG_CE", "LONG")

    @property
    def points(self) -> float:
        assert self.exit is not None
        return (self.exit - self.entry) if self.long else (self.entry - self.exit)

    @property
    def r_multiple(self) -> float:
        return self.points / self.risk_pts if self.risk_pts > 0 else 0.0


@dataclass
class BookResult:
    spec: BookSpec
    trades: list[Trade]
    signals: int
    rejected: Counter[str]


def _ist_time(ts: dt.datetime) -> dt.time:
    return to_ist(ts).time()


def build_views(
    u: UnderlyingData, cal: ExpiryCalendar, oi_window_min: float, oi_k: int, max_chain_age_s: float
) -> list[tuple[MarketView, bool]]:
    """(view, gap_before) per 1-minute bar, identical for every book."""
    tracker = OIPulseTracker(oi_window_min, oi_k, max_chain_age_s)
    for p in u.chain_points:
        tracker.add(p)
    monthly = (
        set(cal.monthly_expiries_between(u.trading_days[0], u.trading_days[-1]))
        if u.trading_days
        else set()
    )
    out: list[tuple[MarketView, bool]] = []
    prev: Bar | None = None
    prev_day: dt.date | None = None
    for b in u.bars:
        d = to_ist(b.ts).date()
        new_session = d != prev_day
        reset = False
        if new_session and prev_day is not None and prev_day in monthly:
            reset = True  # continuous futures switched contract after a monthly expiry
        gap = not new_session and prev is not None and b.ts - prev.ts != M
        now = b.ts + M
        out.append(
            (
                MarketView(
                    underlying=u.underlying,
                    now=now,
                    bar=b,
                    futures_bar=u.futures.get(b.ts),
                    prior_day=u.prior_day(d),
                    oi_pulse=tracker.pulse_at(now) if u.chain_points else None,
                    volume_series_reset=reset,
                    new_session=new_session,
                ),
                gap,
            )
        )
        prev, prev_day = b, d
    return out


class Pricer:
    def __init__(
        self,
        cfg: PlatformConfig,
        ds: Dataset,
        specs: dict[str, Spec],
        cals: dict[str, ExpiryCalendar],
        charges: ChargesTable,
    ) -> None:
        self.cfg, self.ds, self.specs, self.cals, self.charges = cfg, ds, specs, cals, charges
        self.notes: Counter[str] = Counter()

    def _expiry(self, u: str, d: dt.date) -> dt.date:
        rec = self.ds.recorded_expiries.get((u, d))
        policy = self.cfg.contracts.nifty_expiry_day
        if rec:
            future = [e for e in rec if e >= d]
            if not self.specs[u].weekly:  # BANKNIFTY: monthly only
                months = sorted(
                    {
                        e
                        for e in future
                        if e == max(x for x in future if (x.year, x.month) == (e.year, e.month))
                    }
                )
                future = months
            if future:
                e = future[0]
                if e == d and policy == "next_weekly" and len(future) > 1:
                    e = future[1]
                self.notes["expiry_from_recorded_master"] += 1
                return e
        self.notes["expiry_from_rule"] += 1
        return self.cals[u].trading_expiry(d, policy)

    def legs(self, t: Trade) -> None:
        rc, risk = self.cfg.research, self.cfg.risk
        u, d = t.underlying, to_ist(t.entry_at).date()
        spec = self.specs[u]
        call = t.direction == "LONG_CE"
        lot, lot_verified = spec.lot_size(d)
        if not lot_verified:
            self.notes["lot_size_unverified"] += 1
        exp = self._expiry(u, d)
        vix = self.ds.vix_at(t.entry_at)
        cm = rc.costs[u]
        extra = cm.half_spread_points + rc.slippage_ticks_per_side * rc.option_tick
        assert t.exit is not None and t.exit_at is not None
        r = rc.risk_free_rate
        te, tx = years_to_expiry(t.entry_at, exp), years_to_expiry(t.exit_at, exp)
        for scale in rc.iv_sensitivity:
            if vix is None:
                self.notes["no_vix"] += 1
                continue
            iv = vix / 100 * cm.iv_vs_vix * scale
            k = pick_strike(
                t.entry,
                spec.strike_interval,
                self.cfg.contracts.strike_mode,
                call,
                t=te,
                vol=iv,
                r=r,
                delta_target=self.cfg.contracts.delta_target,
            )
            p_in = bs_price(t.entry, k, te, iv, r, call)
            p_stop = bs_price(t.stop, k, te, iv, r, call)
            p_out = bs_price(t.exit, k, tx, iv, r, call)
            entry_prem = p_in + extra
            exit_prem = max(rc.option_tick, p_out - extra)
            t.modeled[scale] = self._price(
                d,
                k,
                exp,
                "CE" if call else "PE",
                lot,
                entry_prem,
                exit_prem,
                iv,
                stop_dist=max(0.0, p_in - p_stop) + 2 * extra,
                risk_inr=float(risk.risk_per_trade_inr),
            )
        key = f"OPT|{u}|{exp.isoformat()}|"
        real = None
        base = t.modeled.get(1.0)
        if base is not None:
            strike_s = f"{Decimal(str(base.strike)).normalize():f}"
            bars = self.ds.option_bars.get(f"{key}{strike_s}|{'CE' if call else 'PE'}")
            if bars and t.entry_at in bars:
                eb = bars[t.entry_at]
                xb = bars.get(t.exit_at) or bars.get(t.exit_at - M)
                if xb is not None:
                    real = self._price(
                        d,
                        base.strike,
                        exp,
                        base.option_type,
                        lot,
                        eb.open + extra,
                        max(rc.option_tick, (xb.open if t.exit_at in bars else xb.close) - extra),
                        None,
                        stop_dist=0.0,
                        risk_inr=0.0,
                    )
        t.real = real

    def _price(
        self,
        d: dt.date,
        strike: float,
        exp: dt.date,
        ot: str,
        lot: int,
        entry_prem: float,
        exit_prem: float,
        iv: float | None,
        *,
        stop_dist: float,
        risk_inr: float,
    ) -> Leg:
        risk, broker = self.cfg.risk, self.cfg.research.execution_broker
        cap = float(risk.capital_inr) * risk.max_premium_pct_of_capital / 100

        def pnl(lots: int) -> tuple[float, float]:
            if lots <= 0:
                return 0.0, 0.0
            q = lots * lot
            ch = self.charges.option_round_trip(
                d,
                broker,
                Decimal(str(round(entry_prem * q, 2))),
                Decimal(str(round(exit_prem * q, 2))),
            )
            c = float(ch["total"])
            return (exit_prem - entry_prem) * q - c, c

        lots, skip = 1, None
        if risk_inr > 0:
            by_risk = math.floor(risk_inr / (stop_dist * lot)) if stop_dist > 0 else 0
            by_cap = math.floor(cap / (entry_prem * lot)) if entry_prem > 0 else 0
            lots = min(by_risk, by_cap, risk.max_lots)
            if by_cap < 1:
                skip = "SKIPPED_SIZE_PREMIUM_CAP"
            elif by_risk < 1:
                skip = "SKIPPED_SIZE_RISK"
            else:
                backstop_loss = self.cfg.contracts.backstop_delta_multiple * stop_dist * lot * lots
                if backstop_loss > risk.max_backstop_loss_multiple_of_risk * risk_inr + 1e-9:
                    skip, lots = "SKIPPED_BACKSTOP", 0
            if skip:
                lots = 0
        net1, ch1 = pnl(1)
        net_s, ch_s = pnl(lots)
        return Leg(
            strike,
            exp,
            ot,
            lot,
            round(entry_prem, 2),
            round(exit_prem, 2),
            iv,
            lots,
            skip,
            net1,
            ch1,
            net_s,
            ch_s,
        )


def simulate(
    cfg: PlatformConfig,
    ds: Dataset,
    books: list[BookSpec],
    views: dict[str, list[tuple[MarketView, bool]]],
    pricer: Pricer,
    date_from: dt.date,
    date_to: dt.date,
) -> list[BookResult]:
    tg = cfg.time_guards
    timeline: list[tuple[dt.datetime, str, int]] = sorted(
        (v.bar.ts, u, i)
        for u, vs in views.items()
        for i, (v, _) in enumerate(vs)
        if date_from <= to_ist(v.bar.ts).date() <= date_to
    )
    results = []
    for spec in books:
        plugin = spec.make_plugin()
        trades: list[Trade] = []
        open_: dict[str, Trade] = {}
        pending: dict[str, Signal] = {}
        rejected: Counter[str] = Counter()
        signals = 0
        trades_today: Counter[dt.date] = Counter()
        last_exit: dt.datetime | None = None
        # Warm-up: feed bars before date_from so indicators are ready on day one.
        for vs in views.values():
            for v, _ in vs:
                if to_ist(v.bar.ts).date() < date_from:
                    plugin.on_candle("1m", v)
        for ts, u, i in timeline:
            view, gap = views[u][i]
            bar, t = view.bar, _ist_time(view.bar.ts)
            d = to_ist(ts).date()
            # 0) the previous day's data ended before 15:10: close at its last price
            tr0 = open_.get(u)
            if tr0 is not None and view.new_session and i > 0:
                last = views[u][i - 1][0].bar
                _close(tr0, last.ts + M, last.close, "EOD_DATA")
                del open_[u]
                last_exit = tr0.exit_at
                pricer.legs(tr0)
                trades.append(tr0)
            # 1) pending entry at this candle's open
            sig = pending.pop(u, None)
            if sig is not None:
                reason = _entry_block(sig, view, t, d, open_, trades_today, last_exit, cfg)
                if reason:
                    rejected[reason] += 1
                else:
                    entry = bar.open
                    stop = float(sig.stop_underlying)
                    long = sig.direction in ("LONG_CE", "LONG")
                    if (long and entry <= stop) or (not long and entry >= stop):
                        rejected["GAPPED_THROUGH_STOP"] += 1
                    else:
                        tr = Trade(
                            spec.name,
                            spec.strategy_id,
                            u,
                            sig.direction,
                            sig.at,
                            bar.ts,
                            entry,
                            stop,
                            float(sig.target_underlying)
                            if sig.target_underlying is not None
                            else None,
                            abs(entry - stop),
                            dict(sig.checklist),
                            sig.config_hash,
                        )
                        _forward(tr, views[u], i, cfg.research.forward_window_min, tg.square_off)
                        open_[u] = tr
                        trades_today[d] += 1
                        plugin.on_trade_opened(sig)
            # 2) manage the open trade on this candle
            cur = open_.get(u)
            if cur is not None:
                tr = cur
                done = _manage(tr, bar, t, tg.square_off)
                if done:
                    del open_[u]
                    last_exit = tr.exit_at
                    pricer.legs(tr)
                    trades.append(tr)
                else:
                    dec = plugin.exit_logic(
                        OpenPosition(
                            tr.direction, tr.entry, tr.stop, tr.target, tr.risk_pts, tr.entry_at
                        ),
                        view,
                    )
                    if dec.action == "TRAIL" and dec.new_stop_underlying is not None:
                        tr.stop = float(dec.new_stop_underlying)
            # 3) new signals at this candle's close
            for s in plugin.on_candle("1m", view):
                signals += 1
                if not spec.accept(s):
                    rejected["FILTERED_BY_BOOK"] += 1
                    continue
                if gap:
                    rejected["DATA_GAP"] += 1
                    continue
                if view.futures_bar is None and s.strategy_id == "ema200_hammer_rvol_oi":
                    rejected["NO_FUTURES_CANDLE"] += 1
                    continue
                pending[u] = s
        for u, tr in list(open_.items()):  # data ended with a trade open
            last = views[u][-1][0].bar
            _close(tr, last.ts + M, last.close, "END_OF_DATA")
            pricer.legs(tr)
            trades.append(tr)
        results.append(BookResult(spec, trades, signals, rejected))
    return results


def _entry_block(
    sig: Signal,
    view: MarketView,
    t: dt.time,
    d: dt.date,
    open_: dict[str, Trade],
    trades_today: Counter[dt.date],
    last_exit: dt.datetime | None,
    cfg: PlatformConfig,
) -> str | None:
    tg, risk = cfg.time_guards, cfg.risk
    if view.bar.ts != sig.at:
        return "NEXT_CANDLE_MISSING"
    if to_ist(sig.at - M).date() != d:
        return "SIGNAL_FROM_PREVIOUS_DAY"
    if tg.no_entry_start <= t < tg.no_entry_end:
        return "TIME_GUARD_OPEN"
    if t > tg.last_entry:
        return "TIME_GUARD_LATE"
    if view.underlying in open_:
        return "POSITION_OPEN"
    if len(open_) >= risk.max_concurrent_positions:
        return "MAX_CONCURRENT"
    if trades_today[d] >= risk.max_trades_per_day:
        return "MAX_TRADES_PER_DAY"
    if last_exit is not None and (view.bar.ts - last_exit) < dt.timedelta(
        minutes=risk.cooldown_minutes_after_exit
    ):
        return "COOLDOWN"
    return None


def _close(tr: Trade, at: dt.datetime, px: float, why: str) -> None:
    tr.exit_at, tr.exit, tr.exit_reason = at, px, why


def _manage(tr: Trade, bar: Bar, t: dt.time, square_off: dt.time) -> bool:
    if t >= square_off:
        _close(tr, bar.ts, bar.open, "SQUARE_OFF")
        return True
    if tr.long:
        tr.mfe_pts = max(tr.mfe_pts, bar.high - tr.entry)
        tr.mae_pts = max(tr.mae_pts, tr.entry - bar.low)
        if bar.open <= tr.stop:
            _close(tr, bar.ts, bar.open, "STOP_GAP")
        elif bar.low <= tr.stop:
            _close(tr, bar.ts + M, tr.stop, "STOP")
        elif tr.target is not None and bar.high >= tr.target:
            _close(tr, bar.ts + M, tr.target, "TARGET")
    else:
        tr.mfe_pts = max(tr.mfe_pts, tr.entry - bar.low)
        tr.mae_pts = max(tr.mae_pts, bar.high - tr.entry)
        if bar.open >= tr.stop:
            _close(tr, bar.ts, bar.open, "STOP_GAP")
        elif bar.high >= tr.stop:
            _close(tr, bar.ts + M, tr.stop, "STOP")
        elif tr.target is not None and bar.low <= tr.target:
            _close(tr, bar.ts + M, tr.target, "TARGET")
    return tr.exit is not None


def _forward(
    tr: Trade, vs: list[tuple[MarketView, bool]], i: int, window_min: int, square_off: dt.time
) -> None:
    """Signal quality independent of the exit: first touch of +2R vs -1R before 15:10, and
    MFE/MAE in R over the next `window_min` minutes."""
    r = tr.risk_pts
    if r <= 0:
        return
    d = to_ist(tr.entry_at).date()
    up, dn = (tr.entry + 2 * r, tr.entry - r) if tr.long else (tr.entry - 2 * r, tr.entry + r)
    mfe = mae = 0.0
    touched = None
    end = tr.entry_at + dt.timedelta(minutes=window_min)
    for v, _ in vs[i:]:
        b = v.bar
        if to_ist(b.ts).date() != d or _ist_time(b.ts) >= square_off:
            break
        if b.ts < end:
            fav = (b.high - tr.entry) if tr.long else (tr.entry - b.low)
            adv = (tr.entry - b.low) if tr.long else (b.high - tr.entry)
            mfe, mae = max(mfe, fav), max(mae, adv)
        if touched is None:
            hit_stop = b.low <= dn if tr.long else b.high >= dn
            hit_tgt = b.high >= up if tr.long else b.low <= up
            if hit_stop:  # stop first when both in one candle (conservative)
                touched = "stop"
            elif hit_tgt:
                touched = "2R"
        if touched is not None and b.ts >= end:
            break
    tr.first_touch = touched or "none"
    tr.fwd_mfe_r, tr.fwd_mae_r = mfe / r, mae / r


def apply_breakers(
    trades: list[Trade], cfg: PlatformConfig, scale: float = 1.0
) -> tuple[list[float], Counter[str]]:
    """Sized net P&L per trade after the daily-loss and consecutive-loss breakers
    (invariant 8). Breakers use realized P&L only (DECISIONS.md D31). A loss-streak trip is
    assumed to be reset by the trader the next trading day."""
    risk = cfg.risk
    cap = float(risk.capital_inr)
    daily_lim = cap * risk.daily_loss.pct_of_capital / 100
    if risk.daily_loss.inr is not None:
        daily_lim = min(daily_lim, float(risk.daily_loss.inr))
    out: list[float] = []
    why: Counter[str] = Counter()
    day_pnl: dict[dt.date, float] = defaultdict(float)
    streak, streak_trip_day = 0, None
    for t in sorted(trades, key=lambda x: x.entry_at):
        leg = t.modeled.get(scale)
        if leg is None or leg.lots_sized == 0:
            continue
        d = to_ist(t.entry_at).date()
        if streak_trip_day is not None and d > streak_trip_day:
            streak, streak_trip_day = 0, None
        if streak_trip_day is not None:
            why["LOSS_STREAK_BREAKER"] += 1
            continue
        if day_pnl[d] <= -daily_lim:
            why["DAILY_LOSS_BREAKER"] += 1
            continue
        out.append(leg.net_sized)
        day_pnl[d] += leg.net_sized
        streak = streak + 1 if leg.net_sized < 0 else 0
        if streak >= risk.max_consecutive_losses:
            streak_trip_day = d
    return out, why


def ist_now() -> dt.datetime:
    return dt.datetime.now(IST)
