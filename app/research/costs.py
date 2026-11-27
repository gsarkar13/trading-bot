"""Itemised charges for an option round trip, from the dated table in config/charges.yaml
(section 17). Never hardcoded: every rate is read from the version in force that day."""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import yaml

CRORE = Decimal(10_000_000)
P = Decimal("0.01")


class ChargesTable:
    def __init__(self, path: Path) -> None:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.versions = sorted(raw["versions"], key=lambda v: v["effective_from"])

    def version(self, d: dt.date) -> dict[str, Any]:
        cur = None
        for v in self.versions:
            if dt.date.fromisoformat(v["effective_from"]) <= d:
                cur = v
        if cur is None:
            # Before the first dated version: use the earliest, and say so in the report.
            cur = self.versions[0]
        return dict(cur)

    def option_round_trip(
        self, d: dt.date, broker: str, buy_value: Decimal, sell_value: Decimal, orders: int = 2
    ) -> dict[str, Decimal]:
        v = self.version(d)

        def pct(x: object) -> Decimal:
            return Decimal(str(x)) / 100

        brk = Decimal(v["brokerage"][broker]["options_per_order_inr"]) * orders
        turnover = buy_value + sell_value
        stt = sell_value * pct(v["stt_pct"]["options_sell_on_premium"])
        txn = turnover * pct(v["exchange_txn_pct"]["options_on_premium"])
        sebi = turnover * Decimal(v["sebi_fee_per_crore_inr"]) / CRORE
        ipft = turnover * Decimal(v.get("ipft_per_crore_inr", "0")) / CRORE
        stamp = buy_value * pct(v["stamp_duty_buy_pct"]["options"])
        gst = (brk + txn + sebi) * pct(v["gst_pct"])
        items = {
            "brokerage": brk,
            "stt": stt,
            "exchange_txn": txn,
            "sebi": sebi,
            "ipft": ipft,
            "stamp": stamp,
            "gst": gst,
        }
        items = {k: x.quantize(P, ROUND_HALF_UP) for k, x in items.items()}
        items["total"] = sum(items.values(), Decimal(0))
        return items
