"""Run backtests and write an HTML report + summary JSON (section 16.4).

    python -m app.tools.backtest run --from 2026-01-01 --to 2026-11-27 --period in-sample --grid
    python -m app.tools.backtest freeze
    python -m app.tools.backtest run --from 2026-01-01 --to 2026-11-27 --period out-of-sample

Periods (set `research.out_of_sample_from` in config/host.yaml or default.yaml first):
  in-sample      dates before out_of_sample_from. The sensitivity grid runs only here.
  out-of-sample  dates on/after it. REFUSED unless the current config hash of every
                 strategy was frozen with `freeze` beforehand (no peeking, then tuning).
  all            whole range, labelled as such (for data checks, not for decisions).

Books: S1_A technical rules only; S1_B with the OI filter; S2_P PDH/PDL plain (baseline);
S2_PT PDH/PDL with the 15-minute trend filter. Book C (AI-approved only) needs recorded AI
verdicts and appears in paper trading (Phase 2), not here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from app.config.loader import REPO_ROOT, config_hash, load_config, trade_config_hash
from app.config.schema import Ema200HammerParams, PdhPdlParams, PlatformConfig
from app.research.backtest import BookResult, BookSpec, Pricer, build_views, simulate
from app.research.contracts import ExpiryCalendar, load_specs
from app.research.costs import ChargesTable
from app.research.dataset import Dataset, load_dataset
from app.research.report import build_summary, render_html
from app.strategies.base import Signal
from app.strategies.ema200_hammer import Ema200HammerRvolOi
from app.strategies.pdh_pdl import PdhPdlBreakout

UNDERLYINGS = ("NIFTY", "BANKNIFTY")
WARMUP_DAYS = 45  # calendar days loaded before the period for indicator warm-up


class OOSRefused(Exception):
    pass


def make_books(
    cfg: PlatformConfig, s1: Ema200HammerParams | None = None, s2: PdhPdlParams | None = None
) -> list[BookSpec]:
    s1 = s1 or cfg.strategy_params("ema200_hammer_rvol_oi")  # type: ignore[assignment]
    s2 = s2 or cfg.strategy_params("pdh_pdl_breakout")  # type: ignore[assignment]
    assert isinstance(s1, Ema200HammerParams) and isinstance(s2, PdhPdlParams)
    h1, h2 = (
        trade_config_hash(cfg, "ema200_hammer_rvol_oi"),
        trade_config_hash(cfg, "pdh_pdl_breakout"),
    )
    if s1 != cfg.strategy_params("ema200_hammer_rvol_oi"):
        h1 = config_hash({"base": h1, "variant": s1.model_dump(mode="json")})
    s2_plain = s2.model_copy(update={"trend_filter_enabled": False})
    s2_trend = s2.model_copy(update={"trend_filter_enabled": True})
    h2p = config_hash({"base": h2, "variant": s2_plain.model_dump(mode="json")})
    h2t = config_hash({"base": h2, "variant": s2_trend.model_dump(mode="json")})
    unknown_passes = s1.oi_unknown_policy == "pass_flagged"

    def oi_ok(s: Signal) -> bool:
        return s.checklist.get("oi") == "CONFIRM" or (
            unknown_passes and s.checklist.get("oi") == "UNKNOWN"
        )

    return [
        BookSpec(
            "S1_A",
            "ema200_hammer_rvol_oi",
            lambda: Ema200HammerRvolOi(s1, h1),
            description="EMA200 hammer + RVOL, technical rules only",
        ),
        BookSpec(
            "S1_B",
            "ema200_hammer_rvol_oi",
            lambda: Ema200HammerRvolOi(s1, h1),
            oi_ok,
            description="S1_A + OI pulse filter (needs recorded chain snapshots)",
        ),
        BookSpec(
            "S2_P",
            "pdh_pdl_breakout",
            lambda: PdhPdlBreakout(s2_plain, h2p),
            description="PDH/PDL breakout, plain (BASELINE)",
        ),
        BookSpec(
            "S2_PT",
            "pdh_pdl_breakout",
            lambda: PdhPdlBreakout(s2_trend, h2t),
            description="PDH/PDL breakout + 15m EMA200 trend filter",
        ),
    ]


def period_bounds(
    cfg: PlatformConfig, period: str, a: dt.date, b: dt.date
) -> tuple[dt.date, dt.date, str]:
    split = cfg.research.out_of_sample_from
    if period == "all":
        return a, b, "ALL DATA (not for decisions)"
    if split is None:
        raise OOSRefused(
            "set research.out_of_sample_from in config first "
            "(the date where out-of-sample data begins)"
        )
    if period == "in-sample":
        return a, min(b, split - dt.timedelta(days=1)), f"IN-SAMPLE (before {split})"
    return max(a, split), b, f"OUT-OF-SAMPLE (from {split})"


def frozen_ok(cfg: PlatformConfig, registry: Path) -> tuple[bool, str]:
    if not registry.exists():
        return False, f"no frozen configs in {registry}; run `freeze` first"
    reg = json.loads(registry.read_text(encoding="utf-8"))
    for sid in ("ema200_hammer_rvol_oi", "pdh_pdl_breakout"):
        h = trade_config_hash(cfg, sid)
        if h not in {e["hash"] for e in reg.get(sid, [])}:
            return False, (
                f"{sid}: current config hash {h} was never frozen; freeze it "
                "(and record it in DECISIONS.md) before out-of-sample runs"
            )
    return True, "ok"


def freeze(cfg: PlatformConfig, registry: Path, now: dt.datetime) -> dict[str, str]:
    reg: dict[str, list[dict[str, str]]] = (
        json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else {}
    )
    out = {}
    for sid in ("ema200_hammer_rvol_oi", "pdh_pdl_breakout"):
        h = trade_config_hash(cfg, sid)
        if h not in {e["hash"] for e in reg.get(sid, [])}:
            reg.setdefault(sid, []).append(
                {
                    "hash": h,
                    "frozen_at": now.isoformat(),
                    "params": json.dumps(cfg.strategy_params(sid).model_dump(mode="json")),
                }
            )
        out[sid] = h
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps(reg, indent=1), encoding="utf-8")
    return out


def run_books(
    cfg: PlatformConfig, ds: Dataset, books: list[BookSpec], a: dt.date, b: dt.date
) -> tuple[list[BookResult], Pricer]:
    specs = load_specs(REPO_ROOT / "config" / "contracts.yaml")
    cals = {
        u: ExpiryCalendar(specs[u], ds.per_underlying[u].trading_days) for u in ds.per_underlying
    }
    s1 = cfg.strategy_params("ema200_hammer_rvol_oi")
    assert isinstance(s1, Ema200HammerParams)
    views = {
        u: build_views(
            d,
            cals[u],
            s1.oi_window_min,
            s1.oi_strikes_each_side,
            cfg.freshness.max_chain_age_s,
            s1.futures_roll_days_before_expiry,
        )
        for u, d in ds.per_underlying.items()
        if d.bars
    }
    pricer = Pricer(cfg, ds, specs, cals, ChargesTable(REPO_ROOT / "config" / "charges.yaml"))
    return simulate(cfg, ds, books, views, pricer, a, b), pricer


def run_grid(cfg: PlatformConfig, ds: Dataset, a: dt.date, b: dt.date) -> dict[str, Any]:
    rc = cfg.research
    s1 = cfg.strategy_params("ema200_hammer_rvol_oi")
    s2 = cfg.strategy_params("pdh_pdl_breakout")
    assert isinstance(s1, Ema200HammerParams) and isinstance(s2, PdhPdlParams)
    cells1, cells2 = [], []
    for rv in rc.grid_rvol:
        for tol in rc.grid_tol:
            for mr in rc.grid_min_range_atr:
                v = s1.model_copy(
                    update={"rvol_threshold": rv, "tol_value": tol, "min_range_atr": mr}
                )
                res, _ = run_books(cfg, ds, make_books(cfg, s1=v)[:1], a, b)
                cells1.append(_cell({"rvol": rv, "tol": tol, "min_range_atr": mr}, res[0]))
    for buf in rc.grid_pdh_buffer:
        v2 = s2.model_copy(update={"buffer_value": buf})
        res, _ = run_books(cfg, ds, make_books(cfg, s2=v2)[2:3], a, b)
        cells2.append(_cell({"buffer": buf}, res[0]))
    return {
        "S1_A": {"cells": cells1, "stability": _stability(cells1)},
        "S2_P": {"cells": cells2, "stability": _stability(cells2)},
    }


def _cell(params: dict[str, float], r: BookResult) -> dict[str, Any]:
    rs = [t.r_multiple for t in r.trades]
    net = [t.modeled[1.0].net_1lot for t in r.trades if 1.0 in t.modeled]
    return {
        **params,
        "trades": len(rs),
        "mean_R": round(sum(rs) / len(rs), 3) if rs else None,
        "net_1lot_per_trade": round(sum(net) / len(net), 1) if net else None,
    }


def _stability(cells: list[dict[str, Any]]) -> dict[str, Any]:
    vals = [c["net_1lot_per_trade"] for c in cells if c["net_1lot_per_trade"] is not None]
    if not vals:
        return {"cells_with_trades": 0}
    return {
        "cells_with_trades": len(vals),
        "share_positive": round(sum(v > 0 for v in vals) / len(vals), 2),
        "median": round(statistics.median(vals), 1),
        "min": min(vals),
        "max": max(vals),
        "note": "judge the whole grid, not its best cell (section 16.4)",
    }


def run(
    engine: Engine,
    cfg: PlatformConfig,
    a: dt.date,
    b: dt.date,
    period: str,
    grid: bool,
    out_dir: Path,
) -> dict[str, Any]:
    lo, hi, label = period_bounds(cfg, period, a, b)
    if period == "out-of-sample":
        ok, why = frozen_ok(cfg, REPO_ROOT / cfg.research.frozen_registry)
        if not ok:
            raise OOSRefused(why)
    if grid and period != "in-sample":
        raise OOSRefused("the sensitivity grid runs on in-sample data only")
    if lo > hi:
        raise OOSRefused(f"empty period {lo}..{hi}")
    ds = load_dataset(
        engine,
        UNDERLYINGS,
        lo - dt.timedelta(days=WARMUP_DAYS),
        hi,
        on_expiry_day=cfg.contracts.nifty_expiry_day,
    )
    if not any(d.bars for d in ds.per_underlying.values()):
        raise OOSRefused("no candles in the database for this range; run backfill first")
    results, pricer = run_books(cfg, ds, make_books(cfg), lo, hi)
    meta = coverage(ds, lo, hi) | {
        "period_label": label,
        "from": lo.isoformat(),
        "to": hi.isoformat(),
        "config_hashes": {
            sid: trade_config_hash(cfg, sid)
            for sid in ("ema200_hammer_rvol_oi", "pdh_pdl_breakout")
        },
        "risk_per_trade_inr": str(cfg.risk.risk_per_trade_inr),
        "capital_inr": str(cfg.risk.capital_inr),
        "max_lots": cfg.risk.max_lots,
        "execution_broker_for_charges": cfg.research.execution_broker,
        "research_assumptions": cfg.research.model_dump(mode="json"),
        "pricing_notes": dict(pricer.notes),
        "warnings": _warnings(cfg),
        "fill_rules": (
            "entry at next candle open; stop checked before target in a candle; "
            "gap beyond stop exits at the open; entries 09:30-14:45 IST; "
            "square-off at the 15:10 candle open; IV held constant within a trade"
        ),
    }
    summary = build_summary(results, cfg, meta)
    g = run_grid(cfg, ds, lo, hi) if grid else None
    if g:
        summary["grid"] = g
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"backtest_{period}_{lo}_{hi}"
    (out_dir / f"{stem}.json").write_text(
        json.dumps(summary, indent=1, default=str), encoding="utf-8"
    )
    (out_dir / f"{stem}.html").write_text(render_html(summary, results, g), encoding="utf-8")
    summary["_files"] = [str(out_dir / f"{stem}.json"), str(out_dir / f"{stem}.html")]
    return summary


def _warnings(cfg: PlatformConfig) -> list[str]:
    w = []
    s1 = cfg.strategy_params("ema200_hammer_rvol_oi")
    assert isinstance(s1, Ema200HammerParams)
    if s1.rvol_source == "option":
        w.append(
            "rvol_source=option: no option volume history is fed to backtests yet, so "
            "Strategy 1 cannot trade in this run; use rvol_source=futures"
        )
    if cfg.contracts.backstop_delta_multiple <= cfg.risk.max_backstop_loss_multiple_of_risk:
        w.append(
            "backstop check is implied by risk sizing with these multiples, so it never "
            "skips a trade (it only bites if backstop_delta_multiple is raised)"
        )
    return w


def coverage(ds: Dataset, a: dt.date, b: dt.date) -> dict[str, Any]:
    from app.core.timeutil import to_ist

    cov: dict[str, Any] = {}
    for u, d in ds.per_underlying.items():
        days = [x for x in d.trading_days if a <= x <= b]
        bars = [x for x in d.bars if a <= to_ist(x.ts).date() <= b]
        fut = sum(1 for x in bars if x.ts in d.futures)
        chain_days = sorted(
            {
                to_ist(p.taken_at).date()
                for p in d.chain_points
                if a <= to_ist(p.taken_at).date() <= b
            }
        )
        cov[u] = {
            "trading_days": len(days),
            "first": str(days[0]) if days else None,
            "last": str(days[-1]) if days else None,
            "index_bars": len(bars),
            "futures_bar_coverage": round(fut / len(bars), 3) if bars else None,
            "days_with_chain_snapshots": len(chain_days),
        }
    cov["vix_1m_bars"] = len(ds.vix_1m)
    cov["option_contracts_with_candles"] = len(ds.option_bars)
    return {"data_coverage": cov}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--from", dest="start", required=True, type=dt.date.fromisoformat)
    r.add_argument("--to", dest="end", required=True, type=dt.date.fromisoformat)
    r.add_argument("--period", choices=("in-sample", "out-of-sample", "all"), default="all")
    r.add_argument("--grid", action="store_true")
    sub.add_parser("freeze")
    a = ap.parse_args(argv)
    from app.db.session import make_engine
    from app.tools.init_db import main as init_db

    cfg = load_config()
    if a.cmd == "freeze":
        hashes = freeze(cfg, REPO_ROOT / cfg.research.frozen_registry, dt.datetime.now(dt.UTC))
        print("Frozen. Add this line to docs/DECISIONS.md BEFORE any out-of-sample run:")
        print(f"  frozen config hashes {dt.datetime.now(dt.UTC):%Y-%m-%d}: {json.dumps(hashes)}")
        return 0
    init_db()
    engine = make_engine(cfg.database.url, cfg.database.sqlite_busy_timeout_ms)
    try:
        s = run(engine, cfg, a.start, a.end, a.period, a.grid, REPO_ROOT / "reports")
    except OOSRefused as e:
        print(f"Refused: {e}")
        return 2
    for name, b in s["books"].items():
        print(
            f"{name:6} trades {b['trades']:4}  {b['verdict_own']}  |  "
            f"{b.get('vs_baseline', {}).get('verdict', 'baseline')}"
        )
    print("\nReport files (attach both in the chat):\n  " + "\n  ".join(s["_files"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
