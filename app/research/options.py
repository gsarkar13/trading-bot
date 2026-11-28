"""Black-Scholes pricing for the MODELED option leg (section 16.4, layer 2).

European, no dividends (index options are European and cash-settled). Time to expiry is
calendar time to 15:30 IST on the expiry day, ACT/365, floored at one minute.
"""

from __future__ import annotations

import datetime as dt
import math

from app.core.timeutil import IST

MIN_T = 1 / (365 * 24 * 60)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def years_to_expiry(now: dt.datetime, expiry: dt.date) -> float:
    exp = dt.datetime.combine(expiry, dt.time(15, 30), IST)
    return max(MIN_T, (exp - now).total_seconds() / (365 * 24 * 3600))


def bs_price(spot: float, strike: float, t: float, vol: float, r: float, call: bool) -> float:
    if vol <= 0 or t <= 0:
        intrinsic = spot - strike if call else strike - spot
        return max(0.0, intrinsic)
    sq = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t) / sq
    d2 = d1 - sq
    disc = math.exp(-r * t)
    if call:
        return spot * _ncdf(d1) - strike * disc * _ncdf(d2)
    return strike * disc * _ncdf(-d2) - spot * _ncdf(-d1)


def bs_delta(spot: float, strike: float, t: float, vol: float, r: float, call: bool) -> float:
    if vol <= 0 or t <= 0:
        return (1.0 if spot > strike else 0.0) if call else (-1.0 if spot < strike else 0.0)
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    return _ncdf(d1) if call else _ncdf(d1) - 1.0


def pick_strike(
    spot: float,
    interval: float,
    mode: str,
    call: bool,
    *,
    t: float = 0.0,
    vol: float = 0.0,
    r: float = 0.0,
    delta_target: float = 0.5,
) -> float:
    atm = round(spot / interval) * interval
    if mode == "ATM":
        return atm
    if mode == "ITM_1":
        return atm - interval if call else atm + interval
    # delta_target: nearest |delta| to the target among ATM +/- 10 strikes
    cands = [atm + k * interval for k in range(-10, 11)]
    return min(cands, key=lambda k: abs(abs(bs_delta(spot, k, t, vol, r, call)) - delta_target))
