import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from app.core.tags import is_foreign, make_tag, parse_tag

inst_ids = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=2, max_size=4)
dates = st.dates(min_value=dt.date(2000, 1, 1), max_value=dt.date(2099, 12, 31))
seqs = st.integers(min_value=0, max_value=999_999)


@given(inst_ids, dates, seqs)
def test_tag_round_trip_and_fits_broker_limits(inst: str, d: dt.date, seq: int) -> None:
    tag = make_tag(inst, d, seq)
    assert tag.isalnum() and 1 <= len(tag) <= 16  # Fyers: 1-30 alphanumerics
    assert parse_tag(tag) == (inst, d, seq)


@given(inst_ids, inst_ids, dates, seqs)
def test_foreign_instance_detected(mine: str, other: str, d: dt.date, seq: int) -> None:
    assert is_foreign(make_tag(other, d, seq), mine) == (other != mine)


def test_untagged_or_manual_orders_are_not_foreign() -> None:
    assert not is_foreign(None, "AWS1")
    assert not is_foreign("", "AWS1")
    assert not is_foreign("manual order", "AWS1")
    assert parse_tag("AWS1999999000001") is None  # invalid date
