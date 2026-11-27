import datetime as dt
from decimal import Decimal, InvalidOperation

import yaml

from app.config.loader import REPO_ROOT


def _leaves(d: object, path: str = "") -> list[tuple[str, object]]:
    if isinstance(d, dict):
        return [x for k, v in d.items() for x in _leaves(v, f"{path}.{k}")]
    return [(path, d)]


def test_charges_table_is_dated_and_exact() -> None:
    data = yaml.safe_load((REPO_ROOT / "config" / "charges.yaml").read_text())
    assert data["versions"]
    for v in data["versions"]:
        dt.date.fromisoformat(v["effective_from"])
        dt.date.fromisoformat(v["checked"])
        assert v["sources"]
        for section in (
            "brokerage",
            "stt_pct",
            "exchange_txn_pct",
            "stamp_duty_buy_pct",
            "dp_charge_per_sell_inr",
        ):
            for path, val in _leaves(v[section], section):
                assert isinstance(val, str), f"{path} must be a quoted decimal string"
                try:
                    Decimal(val)
                except InvalidOperation as e:
                    raise AssertionError(f"{path}={val!r} is not a decimal") from e
