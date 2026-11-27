import datetime as dt
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.adapters.base import OrderRequest
from app.core.tags import make_tag
from app.core.types import InstrumentKey, OptionType, OrderType, Product, Side

OPT = InstrumentKey.option("NIFTY", dt.date(2026, 12, 1), 24000, OptionType.CE)
TAG = make_tag("AWS1", dt.date(2026, 11, 27), 1)


def _req(**kw: object) -> OrderRequest:
    base: dict[str, object] = dict(
        client_tag=TAG,
        key=OPT,
        side=Side.BUY,
        qty=65,
        order_type=OrderType.LMT,
        limit_price=Decimal("120.05"),
        product=Product.MIS,
    )
    base.update(kw)
    return OrderRequest(**base)  # type: ignore[arg-type]


def test_valid_entry_and_protective_stop() -> None:
    _req()
    _req(
        side=Side.SELL,
        order_type=OrderType.SL_LMT,
        limit_price=Decimal("99"),
        trigger_price=Decimal("100"),
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"client_tag": "x"},  # no instance id (inv. 5/16)
        {"client_tag": "AWS1-261127-1"},
        {"key": InstrumentKey.index("NIFTY")},  # not tradable
        {"product": Product.NRML},
        {"trigger_price": Decimal("119")},  # LMT with trigger
        {"order_type": OrderType.SL_LMT},  # SL-LMT without trigger
        {
            "side": Side.SELL,
            "order_type": OrderType.SL_LMT,  # sell stop below its limit
            "limit_price": Decimal("100"),
            "trigger_price": Decimal("99"),
        },
        {"qty": 0},
        {"limit_price": Decimal("0")},
    ],
)
def test_bad_requests_rejected(kw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _req(**kw)


def test_no_market_order_type_exists() -> None:
    assert {t.value for t in OrderType} == {"LMT", "SL-LMT"}
