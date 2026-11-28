"""Dashboard API: login, CSRF header, fresh login for real money, controls, no secrets."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.api.auth import Auth, check_password, hash_password
from app.api.server import ApiContext, create_app
from tests.engine_harness import Harness, activate, ist

PW = "correct horse battery"
H = {"X-Algo": "1"}


def setup(db: Engine, tmp: Path, password: bool = True) -> tuple[Harness, TestClient]:
    activate(db)
    h = Harness(db, tmp)
    auth = Auth(hash_password(PW) if password else None, tmp / "local.token")
    app = create_app(ApiContext(h.cfg, db, h.engine, auth))
    return h, TestClient(app)


def login(c: TestClient) -> None:
    assert c.post("/api/login", json={"password": PW}).status_code == 200


def test_password_hash_roundtrip() -> None:
    h = hash_password("abc12345678")
    assert h.startswith("scrypt$") and "abc12345678" not in h
    assert check_password("abc12345678", h) and not check_password("nope", h)


def test_everything_needs_login_and_posts_need_the_header(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    _, c = setup(migrated_engine, tmp_path)
    assert c.get("/api/status").status_code == 401
    assert c.post("/api/login", json={"password": "wrong"}).status_code == 401
    login(c)
    s = c.get("/api/status").json()
    assert s["engine"]["halted"] is None and s["ceiling"] == "PAPER"
    assert s["host"]["live_possible"] is False
    assert c.post("/api/engine/pause").status_code == 403  # no X-Algo header
    assert c.post("/api/engine/pause", headers=H).json()["halted"].startswith("paused")
    assert c.post("/api/engine/resume", headers=H).json()["halted"] is None


def test_lockout_after_five_wrong_passwords(migrated_engine: Engine, tmp_path: Path) -> None:
    _, c = setup(migrated_engine, tmp_path)
    for _ in range(5):
        c.post("/api/login", json={"password": "x"})
    assert c.post("/api/login", json={"password": PW}).status_code == 401


def test_real_money_needs_a_password_and_a_passed_gate(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    _, c = setup(migrated_engine, tmp_path, password=False)  # localhost, no password
    assert c.get("/api/status").status_code == 200
    r = c.post("/api/arm/request", headers=H)
    assert r.status_code == 403 and "fresh login" in r.json()["detail"]
    _, c2 = setup(migrated_engine, tmp_path / "b")
    login(c2)
    r = c2.post("/api/ceiling", json={"mode": "LIVE"}, headers=H)
    assert r.status_code == 400 and "config ceiling" in r.json()["detail"]
    code = c2.post("/api/arm/request", headers=H).json()["code"]
    assert (
        c2.post(
            "/api/arm/confirm", json={"code": "0000" if code != "0000" else "1111"}, headers=H
        ).status_code
        == 400
    )
    assert c2.post("/api/arm/confirm", json={"code": code}, headers=H).json()["armed"]


def test_flatten_and_stop_buttons(migrated_engine: Engine, tmp_path: Path) -> None:
    h, c = setup(migrated_engine, tmp_path)
    login(c)
    h.strategy.script[ist(10, 0).astimezone(dt.UTC)] = ("LONG_CE", 24000, 23985, 24030)
    asyncio.run(h.minute(10, 0, 24000))
    [uid] = [p.uid for p in h.engine.positions.values() if p.book == "PAPER"]
    h.engine.spot["NIFTY"] = 24010.0
    r = c.post(f"/api/positions/{uid}/stop", json={"action": "-5"}, headers=H)
    assert r.status_code == 400 and "away from the price" in r.json()["detail"]
    assert (
        c.post(f"/api/positions/{uid}/stop", json={"action": "cost"}, headers=H).json()["stop"]
        == 24000
    )
    assert c.post("/api/flatten", json={"phrase": "yes"}, headers=H).status_code == 400
    assert c.post("/api/flatten", json={"phrase": "FLATTEN"}, headers=H).json()["positions"] == 2
    asyncio.run(h.engine.on_timer())
    trades = c.get("/api/trades").json()
    paper = next(t for t in trades if t["book"] == "PAPER")
    assert paper["compliance"] == 75 and paper["overrides"][0]["what"] == "stop cost"
    books = c.get("/api/books").json()
    assert {b["book"] for b in books["books"]} == {"PAPER", "SHADOW:pdh_pdl_breakout"}


def test_strategies_table_and_settings_never_leak_keys(
    migrated_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = "fake-" + "0" * 16 + "wxyz"  # not key-shaped, so the repo scan stays clean
    monkeypatch.setenv("ALGO_ANTHROPIC_API_KEY", fake)
    _, c = setup(migrated_engine, tmp_path)
    login(c)
    rows = c.get("/api/strategies").json()
    pdh = next(r for r in rows if r["id"] == "pdh_pdl_breakout")
    assert pdh["active"] and pdh["effective_mode"] == "PAPER"
    r = c.post("/api/strategies/pdh_pdl_breakout/active", json={"active": False}, headers=H)
    assert r.status_code == 200
    s = c.get("/api/settings")
    assert "0000000000000000" not in s.text and "...wxyz" in s.text
    ev = c.get("/api/events?category=RISK").json()
    assert any("active=False" in e["message"] for e in ev)


def test_local_token_only_from_localhost(tmp_path: Path) -> None:
    a = Auth(None, tmp_path / "t")
    tok = (tmp_path / "t").read_text()
    assert a.local_ok(tok, "127.0.0.1") and not a.local_ok(tok, "100.64.0.2")
    assert not a.local_ok("guess", "127.0.0.1")
