import argparse
import datetime as dt
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from app.tools import phase1


def _reports(tmp: Path) -> Path:
    r = tmp / "reports"
    (r / "probe" / "2026-11-27").mkdir(parents=True)
    (r / "probe" / "2026-11-27" / "history.json").write_text('{"s": "ok"}')
    (r / "backfill").mkdir()
    (r / "backfill" / "summary_a_b.json").write_text('{"series": {}}')
    (r / "backtest_in-sample_a_b.html").write_text("<html>MODELED</html>")
    (r / "backtest_in-sample_a_b.json").write_text('{"books": {}}')
    (r / "recorder").mkdir()
    (r / "recorder" / "2026-11-30.json").write_text('{"app": "MYAPPID-100"}')  # leaked id
    (r / "phase1").mkdir()
    (r / "phase1" / "run_x.log").write_text("login link client_id=MYAPPID-100&state=x\n")
    return r


def test_bundle_includes_results_and_keeps_secrets_out(tmp_path: Path) -> None:
    r = _reports(tmp_path)
    steps = [{"step": "probe", "ok": True}]
    out, man = phase1.make_bundle(
        r,
        steps,
        {"python": "3.12"},
        ["MYAPPID-100"],
        dt.datetime(2026, 11, 28, 16, 0, tzinfo=dt.UTC),
    )
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        log = z.read("phase1/run_x.log").decode()
        manifest = json.loads(z.read("manifest.json"))
        assert "MYAPPID-100" not in log and "<REMOVED>" in log  # our own log: redacted
        for n in names:
            assert "MYAPPID-100" not in z.read(n).decode()
    assert {
        "probe/2026-11-27/history.json",
        "backfill/summary_a_b.json",
        "backtest_in-sample_a_b.html",
        "environment.json",
        "manifest.json",
    } <= names
    assert "recorder/2026-11-30.json" not in names  # another file with a secret: left out
    assert manifest["excluded"] == [
        {"file": "recorder/2026-11-30.json", "why": "contains secret-like text"}
    ]
    assert man["steps"] == steps


def test_bundle_drops_token_like_text(tmp_path: Path) -> None:
    r = _reports(tmp_path)
    jwt = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 10
    (r / "replay").mkdir()
    (r / "replay" / "d.json").write_text(json.dumps({"x": jwt}))
    _, man = phase1.make_bundle(r, [], {}, [], dt.datetime(2026, 11, 28, 16, 1, tzinfo=dt.UTC))
    assert {"file": "replay/d.json", "why": "contains secret-like text"} in man["excluded"]


def _args(**kw: Any) -> argparse.Namespace:
    base = {
        "start": dt.date(2025, 12, 3),
        "end": dt.date(2026, 11, 27),
        "force": False,
        "bundle_only": False,
    }
    return argparse.Namespace(**{**base, **kw})


@pytest.fixture
def fake_steps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    from app.tools import backfill, backtest, fyers_probe, init_db, login

    calls: dict[str, Any] = {"order": []}
    monkeypatch.setattr(backfill, "market_hours", lambda now: False)
    monkeypatch.setattr(init_db, "main", lambda: calls["order"].append("db"))
    monkeypatch.setattr(login, "main", lambda: calls["order"].append("login") or 0)

    def probe() -> int:
        calls["order"].append("probe")
        raise RuntimeError("socket refused")  # a failing step must not stop the rest

    monkeypatch.setattr(fyers_probe, "main", probe)
    monkeypatch.setattr(backfill, "main", lambda argv: calls.__setitem__("bf", argv) or 0)
    monkeypatch.setattr(backtest, "main", lambda argv: calls.__setitem__("bt", argv) or 0)
    monkeypatch.setenv("ALGO_VAULT_PASSPHRASE", "test-passphrase")
    monkeypatch.setenv("ALGO_VAULT_PATH", str(tmp_path / "vault.json"))
    return calls


def test_run_all_continues_after_a_failed_probe(fake_steps: dict[str, Any]) -> None:
    steps: list[dict[str, Any]] = []
    phase1._run_all(_args(), steps)
    assert fake_steps["order"] == ["db", "login", "probe"]  # no token yet -> login ran
    assert [s["ok"] for s in steps] == [True, True, False, True, True]
    assert fake_steps["bf"] == ["--from", "2025-12-03", "--to", "2026-11-27"]
    assert fake_steps["bt"][-3:] == ["--period", "in-sample", "--grid"]


def test_failed_login_skips_fyers_steps(
    fake_steps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tools import login

    monkeypatch.setattr(login, "main", lambda: 1)
    steps: list[dict[str, Any]] = []
    phase1._run_all(_args(), steps)
    assert [s["step"] for s in steps] == ["database setup", "Fyers login"]
    assert "bf" not in fake_steps


def test_refuses_during_market_hours(
    fake_steps: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.tools import backfill

    monkeypatch.setattr(backfill, "market_hours", lambda now: True)
    steps: list[dict[str, Any]] = []
    phase1._run_all(_args(), steps)
    assert steps == [{"step": "time check", "ok": False, "error": "market hours"}]
    assert fake_steps["order"] == []
