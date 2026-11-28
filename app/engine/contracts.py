"""Which option to buy for a signal, and whether it is liquid enough (brief section 9.1).

* Expiry: NIFTY the nearest weekly (on its expiry day: the next weekly, or skip, per
  `contracts.nifty_expiry_day`); BANKNIFTY the current monthly. Always read from the
  instrument master, never computed.
* Strike: ATM, one strike in the money, or the strike nearest a target delta (needs
  greeks; falls back to ATM and says so).
* Liquidity: skip unless the bid-ask spread is at most `max_spread_pct` and the top of
  book shows at least `min_depth_lots` lots on the side we take.
* Premium stop distance: index stop distance x |delta| (delta from the chain's greeks,
  else Black-Scholes with India VIX, else 0.5), used for sizing and the backstop.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from app.adapters.fyers.symbols import SymbolMap
from app.brokers.paper import Depth
from app.config.schema import ContractSelection
from app.core.types import InstrumentKey, OptionType
from app.research.options import bs_delta, years_to_expiry


@dataclass(frozen=True)
class Choice:
    key: InstrumentKey | None
    reason: str  # "ok" or why no contract
    note: str = ""


def choose_contract(
    smap: SymbolMap,
    underlying: str,
    direction: str,
    spot: float,
    now: dt.datetime,
    today: dt.date,
    cfg: ContractSelection,
    deltas: dict[Decimal, float] | None = None,
) -> Choice:
    ot = OptionType.CE if direction == "LONG_CE" else OptionType.PE
    exps = [e for e in smap.option_expiries(underlying) if e >= today]
    if underlying != "NIFTY":
        # monthly only: keep the last expiry of each month
        by_month: dict[tuple[int, int], dt.date] = {}
        for e in exps:
            by_month[(e.year, e.month)] = max(by_month.get((e.year, e.month), e), e)
        exps = sorted(by_month.values())
    if not exps:
        return Choice(None, "no live expiry in the instrument master")
    exp = exps[0]
    if exp == today and underlying == "NIFTY":
        if cfg.nifty_expiry_day == "skip":
            return Choice(None, "expiry day: entries skipped by config")
        if len(exps) < 2:
            return Choice(None, "expiry day and no next weekly listed")
        exp = exps[1]
    strikes = smap.strikes(underlying, exp)
    if not strikes:
        return Choice(None, f"no strikes for {underlying} {exp}")
    atm = min(strikes, key=lambda k: (abs(float(k) - spot), k))
    i = strikes.index(atm)
    note = ""
    if cfg.strike_mode == "ATM":
        k = atm
    elif cfg.strike_mode == "ITM_1":
        j = i - 1 if ot is OptionType.CE else i + 1
        if not 0 <= j < len(strikes):
            return Choice(None, "no in-the-money strike listed")
        k = strikes[j]
    else:
        if not deltas:
            k, note = atm, "delta_target: no greeks, used ATM"
        else:
            k = min(deltas, key=lambda s: abs(abs(deltas[s]) - cfg.delta_target))
    key = InstrumentKey.option(underlying, exp, k, ot)
    try:
        smap.info(key)
    except Exception:
        return Choice(None, f"{key.canonical} not in today's master")
    return Choice(key, "ok", note)


def liquidity_block(depth: Depth | None, lot: int, cfg: ContractSelection) -> str | None:
    if depth is None or depth.bid is None or depth.ask is None or depth.ask <= 0:
        return "no live quote for the option"
    mid = (depth.bid + depth.ask) / 2
    spread_pct = float((depth.ask - depth.bid) / mid * 100) if mid > 0 else 999.0
    if spread_pct > cfg.max_spread_pct:
        return f"spread {spread_pct:.2f}% > {cfg.max_spread_pct}%"
    if depth.ask_qty < cfg.min_depth_lots * lot:
        return f"ask depth {depth.ask_qty} < {cfg.min_depth_lots} lots"
    return None


def estimate_delta(
    key: InstrumentKey,
    spot: float,
    now: dt.datetime,
    vix: float | None,
    chain_delta: float | None,
    r: float = 0.065,
) -> tuple[float, str]:
    if chain_delta is not None:
        return abs(chain_delta), "chain greeks"
    if vix and key.expiry and key.strike is not None:
        t = years_to_expiry(now, key.expiry)
        d = bs_delta(spot, float(key.strike), t, vix / 100, r, key.option_type is OptionType.CE)
        return abs(d), "Black-Scholes with India VIX"
    return 0.5, "no greeks or VIX: assumed 0.5"


def backstop_trigger(
    entry_premium: Decimal,
    premium_stop: float,
    tick: Decimal,
    mode: str,
    delta_multiple: float,
    pct: float,
) -> Decimal:
    """Broker-side SL-LMT trigger on the premium, wider than the strategy stop (9.2)."""
    if mode == "pct":
        raw = entry_premium * (1 - Decimal(str(pct)) / 100)
    else:
        # round the float product first so 1.5 x 14.3 = 21.45, not 21.4500000001
        raw = entry_premium - Decimal(str(round(delta_multiple * premium_stop, 6)))
    steps = int(raw / tick)
    return max(tick, Decimal(steps) * tick)
