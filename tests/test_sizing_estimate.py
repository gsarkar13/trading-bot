from app.tools.sizing_estimate import Underlying, lots_for


def test_skips_when_one_lot_exceeds_risk() -> None:
    u = Underlying(
        "N", lot=65, atm_premium=120, delta=0.5, slippage_per_unit=1.5, stop_points=(30,)
    )
    lots, one_lot_risk, why = lots_for(u, 30, 1000)
    assert one_lot_risk == (30 * 0.5 + 1.5) * 65
    assert lots == 0 and why == "risk budget"
    assert lots_for(u, 30, 1500)[0] == 1


def test_premium_cap_applies() -> None:
    u = Underlying(
        "B", lot=30, atm_premium=700, delta=0.5, slippage_per_unit=4, stop_points=(25,)
    )  # 21,000 > 40% of 50,000
    assert lots_for(u, 25, 5000)[2] == "premium cap"
