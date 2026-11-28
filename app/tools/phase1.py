"""Everything Phase 1 needs from Fyers, in one command, ending in ONE file to attach.

    python -m app.tools.phase1            (Windows: double-click deploy\\windows_phase1.bat)

Run it once after 15:45 IST on a weekday, or at a weekend. It:
  1. asks for your vault passphrase once
  2. logs in to Fyers if today's token is missing (paste the redirect address)
  3. records one real response from each Fyers call (the probe)
  4. downloads about one year of history (resumable: just run it again if it stops)
  5. runs the in-sample backtest plus the sensitivity grid
  6. packs every result into reports/phase1_bundle_<time>.zip  <- attach this file

Each step's outcome is in the bundle's manifest; a failed step does not stop the later
ones unless they depend on it. Nothing secret goes into the bundle: every file is checked,
and any file containing your App ID, secret, token or token-like text is left out.

    python -m app.tools.phase1 --bundle-only     re-pack the latest results (no Fyers calls)
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import platform
import subprocess
import sys
import traceback
import zipfile
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import Any, TextIO

from sqlalchemy import Engine, text

from app.config.loader import REPO_ROOT
from app.core.scrub import find_secrets
from app.core.timeutil import IST

REPORTS = REPO_ROOT / "reports"
HISTORY_DAYS = 360  # Fyers Standard plan keeps ~1 year of 1-minute data (D23)


class Tee(io.TextIOBase):
    """Copy everything printed to the console into a log for the bundle."""

    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, s: str) -> int:
        for st in self.streams:
            st.write(s)
            st.flush()
        return len(s)

    def flush(self) -> None:
        for st in self.streams:
            st.flush()


def known_secret_values() -> list[str]:
    """Exact values that must never leave the machine: .env secrets and today's token."""
    from app.config.secrets import Secrets

    sec = Secrets()
    vals = [
        v.get_secret_value()
        for v in (
            sec.fyers_app_id,
            sec.fyers_app_secret,
            sec.flattrade_api_key,
            sec.flattrade_api_secret,
            sec.flattrade_user_id,
            sec.telegram_bot_token,
            sec.gemini_api_key,
            sec.anthropic_api_key,
        )
        if v
    ]
    pw = os.environ.get("ALGO_VAULT_PASSPHRASE")
    if pw:
        vals.append(pw)
        try:
            from app.adapters.fyers.session import VAULT_KEY
            from app.core.vault import Vault

            raw = Vault(Path(sec.vault_path), pw).get(VAULT_KEY)
            if raw:
                vals.append(str(raw["access_token"]))
        except Exception as e:
            print(f"  note: could not read the token for redaction ({type(e).__name__})")
    return [v for v in vals if len(v) >= 4]


def collect_files(reports: Path) -> list[Path]:
    files: list[Path] = []
    probes = sorted(p for p in (reports / "probe").glob("*") if p.is_dir())
    if probes:
        files += sorted(probes[-1].glob("*.json"))
    for pattern in (
        "backfill/summary_*.json",
        "backtest_*.json",
        "backtest_*.html",
        "recorder/*.json",
        "replay/*.json",
        "phase1/run_*.log",
    ):
        files += sorted(reports.glob(pattern))
    return [f for f in files if f.is_file()]


def environment_report(engine: Engine | None) -> dict[str, Any]:
    env: dict[str, Any] = {
        "generated_at_ist": dt.datetime.now(IST).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "os": platform.platform(),
        "packages": {
            p: _version(p)
            for p in ("fyers-apiv3", "sqlalchemy", "pydantic", "alembic", "cryptography")
        },
        "git_commit": _git_commit(),
    }
    try:
        from app.config.loader import load_config, trade_config_hash

        cfg = load_config()
        env["config_hashes"] = {
            s: trade_config_hash(cfg, s) for s in ("ema200_hammer_rvol_oi", "pdh_pdl_breakout")
        }
        env["out_of_sample_from"] = str(cfg.research.out_of_sample_from)
        env["instance"] = {"id": cfg.instance.instance_id, "role": cfg.instance.instance_role}
    except Exception as e:
        env["config_error"] = str(e)
    if engine is not None:
        env["database"] = database_summary(engine)
    return env


def database_summary(engine: Engine) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with engine.connect() as c:
        out["candles"] = [
            {"key": k, "tf": tf, "source": src, "rows": n, "first": str(a), "last": str(b)}
            for k, tf, src, n, a, b in c.execute(
                text(
                    "SELECT instrument_key, timeframe, source, COUNT(*), MIN(ts), MAX(ts) "
                    "FROM candles WHERE instrument_key NOT LIKE 'OPT|%' "
                    "GROUP BY instrument_key, timeframe, source ORDER BY instrument_key"
                )
            )
        ]
        out["option_contracts_with_candles"] = c.execute(
            text(
                "SELECT COUNT(DISTINCT instrument_key) FROM candles "
                "WHERE instrument_key LIKE 'OPT|%'"
            )
        ).scalar()
        out["ticks"] = c.execute(text("SELECT COUNT(*) FROM ticks")).scalar()
        out["chain_snapshots"] = c.execute(
            text("SELECT COUNT(DISTINCT snapshot_id) FROM option_chain_snapshots")
        ).scalar()
        out["instrument_master_days"] = c.execute(
            text("SELECT COUNT(DISTINCT master_date) FROM instruments")
        ).scalar()
    return out


def _version(pkg: str) -> str | None:
    try:
        return metadata.version(pkg)
    except metadata.PackageNotFoundError:
        return None


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except Exception:
        return None


def make_bundle(
    reports: Path,
    steps: list[dict[str, Any]],
    env: dict[str, Any],
    secrets: list[str],
    now: dt.datetime,
) -> tuple[Path, dict[str, Any]]:
    """Zip the results. Our own run log has known secrets replaced; any other file that
    contains a known secret or token-like text is left out and listed in the manifest."""
    manifest: dict[str, Any] = {"steps": steps, "included": [], "excluded": []}
    out = reports / f"phase1_bundle_{now:%Y%m%d_%H%M}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in collect_files(reports):
            rel = f.relative_to(reports).as_posix()
            body = f.read_text(encoding="utf-8", errors="replace")
            if rel.startswith("phase1/"):
                for s in secrets:
                    body = body.replace(s, "<REMOVED>")
            hit = next((s for s in secrets if s in body), None)
            if hit is not None or find_secrets(body):
                manifest["excluded"].append({"file": rel, "why": "contains secret-like text"})
                continue
            z.writestr(rel, body)
            manifest["included"].append(rel)
        env_text = json.dumps(env, indent=1, default=str)
        z.writestr("environment.json", env_text)
        z.writestr("manifest.json", json.dumps(manifest, indent=1, default=str))
    return out, manifest


def run_step(steps: list[dict[str, Any]], name: str, fn: Callable[[], int | None]) -> bool:
    print(f"\n=== {name} ===")
    started = dt.datetime.now(IST)
    try:
        rc = fn()
        ok = rc in (0, None)
        steps.append(
            {
                "step": name,
                "ok": ok,
                "exit_code": rc,
                "started_ist": started.isoformat(timespec="seconds"),
            }
        )
    except SystemExit as e:
        ok = e.code in (0, None)
        steps.append({"step": name, "ok": ok, "exit_code": e.code})
    except Exception as e:
        ok = False
        steps.append(
            {
                "step": name,
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc(limit=5),
            }
        )
        print(f"  step failed: {type(e).__name__}: {e}")
    print(f"  -> {'OK' if ok else 'FAILED'}")
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    today = dt.datetime.now(IST).date()
    ap.add_argument(
        "--from",
        dest="start",
        type=dt.date.fromisoformat,
        default=today - dt.timedelta(days=HISTORY_DAYS),
    )
    ap.add_argument(
        "--to", dest="end", type=dt.date.fromisoformat, default=today - dt.timedelta(days=1)
    )
    ap.add_argument("--bundle-only", action="store_true")
    ap.add_argument("--force", action="store_true", help="allow the download in market hours")
    a = ap.parse_args(argv)

    from app.config.loader import load_config
    from app.db.session import make_engine

    now = dt.datetime.now(IST)
    (REPORTS / "phase1").mkdir(parents=True, exist_ok=True)
    log_path = REPORTS / "phase1" / f"run_{now:%Y%m%d_%H%M}.log"
    steps: list[dict[str, Any]] = []
    with open(log_path, "w", encoding="utf-8") as log:
        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout = Tee(real_out, log)
        sys.stderr = Tee(real_err, log)
        try:
            if not a.bundle_only:
                _run_all(a, steps)
        finally:
            sys.stdout, sys.stderr = real_out, real_err
    engine = None
    try:
        cfg = load_config()
        engine = make_engine(cfg.database.url, cfg.database.sqlite_busy_timeout_ms)
    except Exception as e:
        print(f"  note: database not readable for the summary ({type(e).__name__})")
    bundle, manifest = make_bundle(
        REPORTS, steps, environment_report(engine), known_secret_values(), now
    )
    failed = [s["step"] for s in steps if not s["ok"]]
    print("\n" + "=" * 60)
    for s in steps:
        print(f"  {'OK    ' if s['ok'] else 'FAILED'}  {s['step']}")
    if manifest["excluded"]:
        print(f"  ({len(manifest['excluded'])} file(s) left out for containing secret-like text)")
    print(f"\nAttach this ONE file in the chat:\n  {bundle}")
    return 1 if failed else 0


def _run_all(a: argparse.Namespace, steps: list[dict[str, Any]]) -> None:
    from app.adapters.base import AuthExpiredError
    from app.adapters.fyers.session import load_token
    from app.config.secrets import Secrets
    from app.core.vault import ENV_VAR, Vault, get_passphrase
    from app.tools import backfill, backtest, fyers_probe, init_db, login
    from app.tools.backfill import market_hours

    if market_hours(dt.datetime.now(dt.UTC)) and not a.force:
        print(
            "It is market hours (09:00-15:45 IST). Run this after 15:45 or at a weekend,\n"
            "so the download doesn't use up the recorder's daily Fyers request quota."
        )
        steps.append({"step": "time check", "ok": False, "error": "market hours"})
        return
    run_step(steps, "database setup", init_db.main)
    sec = Secrets()
    vault_path = Path(sec.vault_path)
    os.environ[ENV_VAR] = get_passphrase(confirm_new=not vault_path.exists())

    def ensure_login() -> int:
        try:
            tok = load_token(Vault(vault_path, os.environ[ENV_VAR]))
            print(f"  Fyers token valid until {tok.expires_at.astimezone(IST):%Y-%m-%d %H:%M} IST")
            return 0
        except AuthExpiredError:
            return login.main()

    if not run_step(steps, "Fyers login", ensure_login):
        print("Login failed, so the Fyers steps are skipped. Fix the login and run again.")
        return
    run_step(steps, "probe (one real response per Fyers call)", fyers_probe.main)
    run_step(
        steps,
        f"download history {a.start}..{a.end}",
        lambda: backfill.main(
            [
                "--from",
                a.start.isoformat(),
                "--to",
                a.end.isoformat(),
                *(["--force"] if a.force else []),
            ]
        ),
    )
    run_step(
        steps,
        "in-sample backtest + sensitivity grid",
        lambda: backtest.main(
            [
                "run",
                "--from",
                a.start.isoformat(),
                "--to",
                a.end.isoformat(),
                "--period",
                "in-sample",
                "--grid",
            ]
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
