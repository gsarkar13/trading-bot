import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.adapters.base import (
    AdapterError,
    AuthExpiredError,
    InstrumentMappingError,
    RateLimitedError,
)
from app.adapters.fyers import auth
from app.adapters.fyers.chain import parse_chain
from app.adapters.fyers.client import FyersClient, check, chunks, parse_candles
from app.adapters.fyers.symbols import SymbolMap, parse_master
from app.adapters.fyers.ws import parse_tick
from app.core.ratelimit import RateLimiter
from app.core.timeutil import IST
from app.core.types import InstrumentKey, OptionType
from app.core.vault import Vault, VaultError

FIX = Path(__file__).parent / "fixtures" / "fyers"


@pytest.fixture(scope="module")
def smap() -> SymbolMap:
    return SymbolMap(
        parse_master(
            (FIX / "NSE_FO_sample.csv").read_text() + (FIX / "NSE_CM_sample.csv").read_text()
        )
    )


# ------------------------------------------------------------------ login
def test_redirect_parsing_uses_auth_code_not_code_200() -> None:
    url = (
        "https://trade.fyers.in/api-login/redirect-uri/index.html?s=ok&code=200"
        "&auth_code=AUTHCODE123&state=st8"
    )
    assert auth.parse_redirect(url, "st8") == "AUTHCODE123"
    assert auth.parse_redirect("s=ok&code=200&auth_code=X1&state=st8", "st8") == "X1"


@pytest.mark.parametrize(
    "url, err",
    [
        ("https://x/?s=ok&code=200&state=st8", "no auth_code"),
        ("https://x/?s=error&code=-16&message=bad&state=st8", "Fyers reported an error"),
        ("https://x/?s=ok&auth_code=A&state=other", "state does not match"),
    ],
)
def test_redirect_parsing_errors(url: str, err: str) -> None:
    with pytest.raises(auth.LoginError, match=err):
        auth.parse_redirect(url, "st8")


def test_app_id_hash_matches_sdk() -> None:
    from fyers_apiv3 import fyersModel

    s = fyersModel.SessionModel(client_id="ABCD-100", secret_key="sek")
    assert auth.app_id_hash("ABCD-100", "sek") == s.get_hash().hexdigest()


def test_login_url_has_required_params() -> None:
    u = auth.login_url("ABCD-100", "https://r/x", "st8")
    assert u.startswith("https://api-t1.fyers.in/api/v3/generate-authcode?")
    assert "response_type=code" in u and "state=st8" in u and "client_id=ABCD-100" in u


def test_token_expiry_is_next_0630_ist() -> None:
    before = dt.datetime(2026, 11, 27, 6, 0, tzinfo=IST)
    after = dt.datetime(2026, 11, 27, 8, 45, tzinfo=IST)
    assert auth.token_expiry(before) == dt.datetime(2026, 11, 27, 6, 30, tzinfo=IST)
    assert auth.token_expiry(after) == dt.datetime(2026, 11, 28, 6, 30, tzinfo=IST)
    tok = auth.StoredToken("t" * 40, "A-100", after, auth.token_expiry(after))
    assert "t" * 10 not in repr(tok)
    with pytest.raises(AuthExpiredError):
        auth.require_valid(tok, dt.datetime(2026, 11, 28, 7, 0, tzinfo=IST))
    with pytest.raises(AuthExpiredError):
        auth.require_valid(None, after)


# ------------------------------------------------------------------ vault
def test_vault_round_trip_and_wrong_passphrase(tmp_path: Path) -> None:
    v = Vault(tmp_path / "vault.json", "correct horse")
    v.put("fyers", {"access_token": "secret-token-value"})
    raw = (tmp_path / "vault.json").read_text()
    assert "secret-token-value" not in raw  # encrypted at rest
    assert Vault(tmp_path / "vault.json", "correct horse").get("fyers") == {
        "access_token": "secret-token-value"
    }
    with pytest.raises(VaultError, match="wrong passphrase"):
        Vault(tmp_path / "vault.json", "wrong pass").get("fyers")


# ------------------------------------------------------------------ rate limit
def test_rate_limiter_per_second_and_minute() -> None:
    now = [0.0]
    rl = RateLimiter(per_second=3, per_minute=5, clock=lambda: now[0])
    for _ in range(3):
        assert rl.wait_time() == 0
        rl.record()
    assert rl.wait_time() == pytest.approx(1.0)  # 4th call in the same second waits
    now[0] = 1.0
    rl.record()
    rl.record()
    now[0] = 2.0
    assert rl.wait_time() == pytest.approx(58.0)  # 5 calls in the last minute


# ------------------------------------------------------------------ REST
@pytest.mark.parametrize(
    "resp, exc",
    [
        ({"s": "error", "code": -16, "message": "x"}, AuthExpiredError),
        ({"s": "error", "code": -429, "message": "x"}, RateLimitedError),
        ({"s": "error", "code": -300, "message": "x"}, InstrumentMappingError),
        ({"s": "error", "code": -99, "message": "Invalid symbol"}, InstrumentMappingError),
        ({"s": "error", "code": -50, "message": "bad"}, AdapterError),
        ("not a dict", AdapterError),
    ],
)
def test_error_mapping(resp: Any, exc: type[Exception]) -> None:
    with pytest.raises(exc):
        check(resp, "t")


def test_parse_candles_and_chunks() -> None:
    resp = {
        "s": "ok",
        "candles": [
            [1796010900, 24000, 24010.5, 23990, 24005, 1200],
            [1796010960, 24005, 24006, 24001, 24002, 900, 55000],
        ],
    }
    bars = parse_candles(resp)
    assert bars[0].ts == dt.datetime(2026, 11, 30, 3, 55, tzinfo=dt.UTC)
    assert (bars[0].high, bars[0].volume, bars[1].oi) == (24010.5, 1200, 55000)
    c = chunks(dt.date(2026, 1, 1), dt.date(2026, 12, 31), 100)
    assert c[0] == (dt.date(2026, 1, 1), dt.date(2026, 4, 10)) and c[-1][1] == dt.date(2026, 12, 31)
    assert all((b - a).days + 1 <= 100 for a, b in c)


class FakeModel:
    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        self.calls: list[dict[str, Any]] = []

    def history(self, data: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(data)
        return {"s": "ok", "candles": []}


def test_client_refuses_oversized_range_and_sends_documented_params(tmp_path: Path) -> None:
    made: list[FakeModel] = []

    def factory(**kw: Any) -> FakeModel:
        made.append(FakeModel(**kw))
        return made[-1]

    c = FyersClient("A-100", "tok", RateLimiter(100, 1000), tmp_path, factory)
    assert made[0].kw["log_path"] == str(tmp_path)  # SDK logs kept out of the repo
    with pytest.raises(ValueError):
        c.history("NSE:NIFTY50-INDEX", "1", dt.date(2026, 1, 1), dt.date(2026, 6, 1))
    c.history("NSE:NIFTY26DECFUT", "1", dt.date(2026, 11, 1), dt.date(2026, 11, 26), cont_flag=True)
    sent = made[0].calls[0]
    assert sent["cont_flag"] == "1" and sent["date_format"] == "1" and sent["resolution"] == "1"


# ------------------------------------------------------------------ chain + ticks
def _chain_resp() -> dict[str, Any]:
    def row(strike: int, ot: str, oi: int) -> dict[str, Any]:
        return {
            "symbol": f"NSE:NIFTY26D01{strike}{ot}",
            "strike_price": strike,
            "option_type": ot,
            "bid": 120.5,
            "ask": 121.0,
            "ltp": 120.8,
            "volume": 1000,
            "oi": oi,
            "oich": 50,
        }

    return {
        "s": "ok",
        "code": 200,
        "data": {
            "optionsChain": [
                {"symbol": "NSE:NIFTY50-INDEX", "strike_price": -1, "ltp": 24010.3},
                row(24000, "CE", 10000),
                row(24000, "PE", 12000),
                {"symbol": "NSE:NOTINMASTER", "strike_price": 1, "option_type": "CE"},
            ],
            "expiryData": [{"date": "01-12-2026", "expiry": "1796119800"}],
            "indiavixData": {"ltp": 13.2},
        },
    }


def test_parse_chain(smap: SymbolMap) -> None:
    snap = parse_chain(_chain_resp(), "NIFTY", smap, dt.datetime(2026, 11, 30, 4, tzinfo=dt.UTC))
    assert snap.spot == Decimal("24010.3") and snap.india_vix == Decimal("13.2")
    assert snap.expiry == dt.date(2026, 12, 1) and len(snap.rows) == 2  # unknown row skipped
    assert snap.rows[1].key == InstrumentKey.option(
        "NIFTY", dt.date(2026, 12, 1), 24000, OptionType.PE
    )
    assert snap.available_expiries == (dt.date(2026, 12, 1),)


def test_parse_tick_uses_exchange_time(smap: SymbolMap) -> None:
    recv = dt.datetime(2026, 11, 30, 4, 0, 5, tzinfo=dt.UTC)
    t = parse_tick(
        {
            "type": "sf",
            "symbol": "NSE:NIFTY26DECFUT",
            "ltp": 24050.5,
            "vol_traded_today": 123456,
            "exch_feed_time": 1796010901,
            "last_traded_time": 1796010900,
            "bid_price": 24050,
            "ask_price": 24051,
            "bid_size": 65,
            "ask_size": 130,
        },
        smap,
        recv,
    )
    assert t is not None and t.exchange_ts == dt.datetime(2026, 11, 30, 3, 55, 1, tzinfo=dt.UTC)
    assert t.cum_volume == 123456 and t.key.kind == "FUT" and t.received_ts == recv
    idx = parse_tick(
        {"type": "if", "symbol": "NSE:NIFTY50-INDEX", "ltp": 24001.1, "exch_feed_time": 1796010902},
        smap,
        recv,
    )
    assert idx is not None and idx.key == InstrumentKey.index("NIFTY") and idx.cum_volume is None
    assert parse_tick({"type": "cn", "code": 200}, smap, recv) is None
    assert (
        parse_tick(
            {"type": "sf", "symbol": "NSE:UNKNOWN", "ltp": 1, "exch_feed_time": 1}, smap, recv
        )
        is None
    )
    assert parse_tick({"type": "if", "symbol": "NSE:NIFTY50-INDEX", "ltp": 1}, smap, recv) is None


def _fake_jwt(claims: dict[str, Any]) -> str:
    import base64
    import json

    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return f"{enc({'alg': 'HS256'})}.{enc(claims)}.c2lnbmF0dXJl"


def test_bare_auth_code_from_the_fyers_page_is_accepted() -> None:
    now = dt.datetime(2026, 11, 28, 11, 0, tzinfo=dt.UTC)
    code = _fake_jwt({"sub": "auth_code", "exp": int(now.timestamp()) + 600})
    assert auth.parse_redirect(code, "st8", now) == code
    assert auth.parse_redirect(f"  {code}\n", "st8", now) == code
    with pytest.raises(auth.LoginError, match="expired"):
        auth.parse_redirect(_fake_jwt({"sub": "auth_code", "exp": 1}), "st8", now)
    with pytest.raises(auth.LoginError, match="not a Fyers auth code"):
        auth.parse_redirect(_fake_jwt({"sub": "access_token"}), "st8", now)  # wrong kind
    with pytest.raises(auth.LoginError, match="no auth_code found"):
        auth.parse_redirect("hello", "st8", now)
