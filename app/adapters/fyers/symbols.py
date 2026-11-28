"""Fyers symbol master: parse it and map internal keys <-> Fyers symbols (section 14.3).

Source: https://public.fyers.in/sym_details/NSE_FO.csv and NSE_CM.csv (public, no login).
Column layout (BROKER_NOTES.md, verified against the real file on 2026-11-27):
  0 fytoken, 1 description, 2 instrument type, 3 lot size, 4 tick size, 5 ISIN,
  6 session, 7 last update, 8 expiry (epoch), 9 symbol ticker, 10 exchange, 11 segment,
  12 scrip code, 13 underlying, 14 underlying scrip code, 15 strike, 16 option type, ...
Instrument types seen: 0 equity, 10 index, 11 index future, 13 stock future,
14 index option, 15 stock option.

We never build Fyers symbol strings ourselves; every symbol comes from this file.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import itertools
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.adapters.base import InstrumentInfo, InstrumentMappingError
from app.core.timeutil import IST
from app.core.types import InstrumentKey, OptionType

MASTER_URLS = {
    "NSE_FO": "https://public.fyers.in/sym_details/NSE_FO.csv",
    "NSE_CM": "https://public.fyers.in/sym_details/NSE_CM.csv",
}
TYPE_EQ, TYPE_INDEX, TYPE_FUTIDX, TYPE_OPTIDX = 0, 10, 11, 14


@dataclass(frozen=True)
class MasterRow:
    info: InstrumentInfo
    instrument_type: int


def _expiry_date(epoch: str) -> dt.date:
    return dt.datetime.fromtimestamp(int(epoch), IST).date()


def parse_row(row: list[str]) -> MasterRow | None:
    """Return a mapped row, or None for instrument types we don't use."""
    if len(row) < 17:
        raise InstrumentMappingError(f"short symbol-master row ({len(row)} cols)")
    itype = int(row[2])
    ticker = row[9]
    underlying = row[13].strip().upper()
    lot = int(row[3] or 0)
    tick = Decimal(row[4])
    if itype == TYPE_OPTIDX:
        key = InstrumentKey.option(
            underlying, _expiry_date(row[8]), Decimal(row[15]).normalize(), OptionType(row[16])
        )
    elif itype == TYPE_FUTIDX:
        key = InstrumentKey.future(underlying, _expiry_date(row[8]))
    elif itype == TYPE_INDEX:
        key = InstrumentKey.index(underlying)
    elif itype == TYPE_EQ and ticker.endswith("-EQ") and row[5]:
        key = InstrumentKey.equity(row[5], "EQ")
    else:
        return None
    info = InstrumentInfo(
        key=key, broker_symbol=ticker, broker_token=row[0], lot_size=max(lot, 1), tick_size=tick
    )
    return MasterRow(info=info, instrument_type=itype)


def parse_master(text: str) -> list[InstrumentInfo]:
    out = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        parsed = parse_row(row)
        if parsed is not None:
            out.append(parsed.info)
    return out


def download_master(segment: str, timeout: float = 60) -> str:
    url = MASTER_URLS[segment]
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
        data: bytes = resp.read()
    return data.decode("utf-8")


class SymbolMap:
    """Two-way map between InstrumentKey and Fyers symbol, plus contract queries."""

    def __init__(self, infos: Iterable[InstrumentInfo]) -> None:
        self._by_key: dict[InstrumentKey, InstrumentInfo] = {}
        self._by_symbol: dict[str, InstrumentInfo] = {}
        for i in infos:
            if i.key in self._by_key and self._by_key[i.key].broker_symbol != i.broker_symbol:
                raise InstrumentMappingError(f"two symbols for {i.key.canonical}")
            self._by_key[i.key] = i
            self._by_symbol[i.broker_symbol] = i

    def __len__(self) -> int:
        return len(self._by_key)

    def info(self, key: InstrumentKey) -> InstrumentInfo:
        try:
            return self._by_key[key]
        except KeyError:
            raise InstrumentMappingError(f"no Fyers symbol for {key.canonical}") from None

    def symbol(self, key: InstrumentKey) -> str:
        return self.info(key).broker_symbol

    def key_for(self, symbol: str) -> InstrumentKey:
        try:
            return self._by_symbol[symbol].key
        except KeyError:
            raise InstrumentMappingError(f"unknown Fyers symbol {symbol!r}") from None

    def option_expiries(self, underlying: str) -> list[dt.date]:
        return sorted(
            {
                k.expiry
                for k in self._by_key
                if k.kind == "OPT" and k.underlying == underlying and k.expiry
            }
        )

    def future_expiries(self, underlying: str) -> list[dt.date]:
        return sorted(
            {
                k.expiry
                for k in self._by_key
                if k.kind == "FUT" and k.underlying == underlying and k.expiry
            }
        )

    def strikes(self, underlying: str, expiry: dt.date) -> list[Decimal]:
        return sorted(
            {
                k.strike
                for k in self._by_key
                if k.kind == "OPT"
                and k.underlying == underlying
                and k.expiry == expiry
                and k.strike is not None
            }
        )

    def strike_interval(self, underlying: str, expiry: dt.date) -> Decimal:
        """Most common gap between adjacent strikes (read from the master, never hardcoded)."""
        s = self.strikes(underlying, expiry)
        if len(s) < 2:
            raise InstrumentMappingError(f"not enough strikes for {underlying} {expiry}")
        gaps: dict[Decimal, int] = {}
        for a, b in itertools.pairwise(s):
            gaps[b - a] = gaps.get(b - a, 0) + 1
        return max(gaps.items(), key=lambda kv: (kv[1], -kv[0]))[0]

    def atm_strike(self, underlying: str, expiry: dt.date, spot: float) -> Decimal:
        s = self.strikes(underlying, expiry)
        if not s:
            raise InstrumentMappingError(f"no strikes for {underlying} {expiry}")
        return min(s, key=lambda k: (abs(float(k) - spot), k))

    def front_future(
        self, underlying: str, today: dt.date, roll_days: int, trading_days_to: object = None
    ) -> InstrumentKey:
        """Current-month future, rolled to the next one `roll_days` calendar-trading days
        before expiry (section 7.3). `trading_days_to(a, b)` counts trading days; default
        counts weekdays."""
        exps = [e for e in self.future_expiries(underlying) if e >= today]
        if not exps:
            raise InstrumentMappingError(f"no live futures for {underlying}")

        def wd(a: dt.date, b: dt.date) -> int:
            n, d = 0, a
            while d < b:
                d += dt.timedelta(days=1)
                n += d.weekday() < 5
            return n

        count = trading_days_to if callable(trading_days_to) else wd
        first = exps[0]
        if count(today, first) < roll_days and len(exps) > 1:
            first = exps[1]
        return InstrumentKey.future(underlying, first)
