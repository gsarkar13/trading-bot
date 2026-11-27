"""Parse a Fyers option-chain response into our ChainSnapshot (section 7.4).

Response shape (docs, BROKER_NOTES.md): data.optionsChain[] (first element is the
underlying with strike_price -1), data.expiryData[], data.indiavixData. Some SDK versions
return the fields at top level, so both are accepted. UNVERIFIED against a live response
until the trader's probe fixture is committed (tests/fixtures/fyers/recorded/).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from app.adapters.base import ChainRow, ChainSnapshot, InstrumentMappingError
from app.adapters.fyers.symbols import SymbolMap
from app.core.timeutil import IST
from app.core.types import OptionType


def _num(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v))


def _int(v: Any) -> int | None:
    return None if v is None or v == "" else int(v)


def parse_chain(
    resp: dict[str, Any], underlying: str, smap: SymbolMap, taken_at: dt.datetime
) -> ChainSnapshot:
    data = resp.get("data", resp)
    rows_in = data.get("optionsChain") or []
    spot = None
    rows: list[ChainRow] = []
    expiry: dt.date | None = None
    for r in rows_in:
        if float(r.get("strike_price", -1)) < 0:
            spot = _num(r.get("ltp"))
            continue
        try:
            key = smap.key_for(r["symbol"])
        except InstrumentMappingError:
            continue  # contract not in today's master: skip, never guess
        expiry = key.expiry
        g = r.get("greeks") or {}
        rows.append(
            ChainRow(
                strike=Decimal(str(r["strike_price"])).normalize(),
                option_type=OptionType(r["option_type"]),
                key=key,
                bid=_num(r.get("bid")),
                ask=_num(r.get("ask")),
                ltp=_num(r.get("ltp")),
                volume=_int(r.get("volume")),
                oi=_int(r.get("oi")),
                oi_change=_int(r.get("oich")),
                iv=_num(g.get("iv", r.get("iv"))),
                delta=_num(g.get("delta", r.get("delta"))),
            )
        )
    if expiry is None:
        raise InstrumentMappingError(f"empty option chain for {underlying}")
    vix = data.get("indiavixData") or {}
    expiries = tuple(
        sorted(
            dt.datetime.fromtimestamp(int(e["expiry"]), IST).date()
            for e in data.get("expiryData") or []
            if e.get("expiry")
        )
    )
    return ChainSnapshot(
        underlying=underlying,
        expiry=expiry,
        taken_at=taken_at,
        spot=spot,
        india_vix=_num(vix.get("ltp")),
        rows=tuple(rows),
        available_expiries=expiries,
    )
