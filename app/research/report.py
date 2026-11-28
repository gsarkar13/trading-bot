"""Summaries and a self-contained HTML report for a backtest run (section 16.4)."""

from __future__ import annotations

import datetime as dt
import html
import json
from collections import Counter, defaultdict
from typing import Any

from app.config.schema import PlatformConfig
from app.core.timeutil import to_ist
from app.research.backtest import BookResult, Trade, apply_breakers
from app.research.stats import diff_ci, summarize, verdict_vs_baseline

BASELINE_BOOK = "S2_P"


def _r(x: float | None, n: int = 2) -> float | None:
    return None if x is None else round(x, n)


def layer1(trades: list[Trade], samples: int) -> dict[str, Any]:
    rs = [t.r_multiple for t in trades]
    ft = Counter(t.first_touch for t in trades)
    n = len(trades)
    by_hour: dict[str, list[Trade]] = defaultdict(list)
    by_wd: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        loc = to_ist(t.entry_at)
        by_hour[f"{loc.hour:02d}:00"].append(t)
        by_wd[loc.strftime("%a")].append(t)

    def bucket(ts: list[Trade]) -> dict[str, Any]:
        return {
            "n": len(ts),
            "mean_r": _r(sum(t.r_multiple for t in ts) / len(ts), 3),
            "first_touch_2R": _r(sum(t.first_touch == "2R" for t in ts) / len(ts), 3),
        }

    return {
        "r_multiple": summarize(rs, samples).as_dict(),
        "first_touch_2R_before_stop": _r(ft["2R"] / n, 3) if n else None,
        "first_touch_stop_first": _r(ft["stop"] / n, 3) if n else None,
        "first_touch_neither": _r(ft["none"] / n, 3) if n else None,
        "break_even_rate_for_2R": 0.333,
        "hit_1R_in_trade": _r(sum(t.mfe_pts >= t.risk_pts for t in trades) / n, 3) if n else None,
        "hit_2R_in_trade": _r(sum(t.mfe_pts >= 2 * t.risk_pts for t in trades) / n, 3)
        if n
        else None,
        "avg_mfe_r_fwd": _r(sum(t.fwd_mfe_r for t in trades) / n, 3) if n else None,
        "avg_mae_r_fwd": _r(sum(t.fwd_mae_r for t in trades) / n, 3) if n else None,
        "avg_risk_points": _r(sum(t.risk_pts for t in trades) / n) if n else None,
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
        "by_hour": {k: bucket(v) for k, v in sorted(by_hour.items())},
        "by_weekday": {k: bucket(v) for k, v in by_wd.items()},
    }


def layer2(trades: list[Trade], cfg: PlatformConfig, samples: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for scale in cfg.research.iv_sensitivity:
        legs = [t.modeled[scale] for t in trades if scale in t.modeled]
        sized, breaker = apply_breakers(trades, cfg, scale)
        skips = Counter(leg.skip_reason for leg in legs if leg.skip_reason)
        out[f"iv_x{scale}"] = {
            "one_lot_net_inr": summarize([x.net_1lot for x in legs], samples).as_dict(),
            "avg_charges_1lot_inr": _r(sum(x.charges_1lot for x in legs) / len(legs))
            if legs
            else None,
            "avg_entry_premium": _r(sum(x.entry_premium for x in legs) / len(legs))
            if legs
            else None,
            "sized_net_inr_after_breakers": summarize(sized, samples).as_dict(),
            "skipped_for_size": dict(skips),
            "skipped_share": _r(sum(skips.values()) / len(legs), 3) if legs else None,
            "breaker_skips": dict(breaker),
        }
    return out


def layer3(trades: list[Trade], samples: int) -> dict[str, Any]:
    legs = [t.real for t in trades if t.real is not None]
    return {
        "trades_with_real_option_data": len(legs),
        "of_total": len(trades),
        "one_lot_net_inr": summarize([x.net_1lot for x in legs], samples).as_dict(),
    }


def build_summary(
    results: list[BookResult], cfg: PlatformConfig, meta: dict[str, Any]
) -> dict[str, Any]:
    samples = cfg.research.bootstrap_samples
    books: dict[str, Any] = {}
    base = next((r for r in results if r.spec.name == BASELINE_BOOK), None)
    base_vals = [t.modeled[1.0].net_1lot for t in base.trades if 1.0 in t.modeled] if base else []
    base_r = [t.r_multiple for t in base.trades] if base else []
    for r in results:
        vals = [t.modeled[1.0].net_1lot for t in r.trades if 1.0 in t.modeled]
        rr = [t.r_multiple for t in r.trades]
        entry: dict[str, Any] = {
            "description": r.spec.description,
            "strategy_id": r.spec.strategy_id,
            "signals": r.signals,
            "trades": len(r.trades),
            "rejected": dict(r.rejected),
            "config_hashes": sorted({t.config_hash for t in r.trades}),
            "layer1_underlying": layer1(r.trades, samples),
            "layer2_MODELED_option": layer2(r.trades, cfg, samples),
            "layer3_REAL_option": layer3(r.trades, samples),
        }
        own = summarize(vals, samples)
        entry["verdict_own"] = (
            "not enough trades"
            if own.n < 2
            else "positive expectancy after costs (MODELED)"
            if (own.ci90_low or 0) > 0
            else "no positive expectancy after costs (MODELED)"
            if (own.expectancy or 0) <= 0
            else "positive but not distinguishable from zero (MODELED)"
        )
        if r.spec.name != BASELINE_BOOK and base is not None:
            d, lo, hi = diff_ci(vals, base_vals, samples)
            dr, lor, hir = diff_ci(rr, base_r, samples)
            entry["vs_baseline"] = {
                "layer2_one_lot_expectancy_diff_inr": _r(d),
                "ci90": [_r(lo), _r(hi)],
                "layer1_mean_r_diff": _r(dr, 3),
                "ci90_r": [_r(lor, 3), _r(hir, 3)],
                "verdict": verdict_vs_baseline(d, lo),
            }
        books[r.spec.name] = entry
    return {"meta": meta, "books": books}


def trade_rows(results: list[BookResult]) -> list[dict[str, Any]]:
    rows = []
    for r in results:
        for t in r.trades:
            m = t.modeled.get(1.0)
            rows.append(
                {
                    "book": r.spec.name,
                    "underlying": t.underlying,
                    "direction": t.direction,
                    "entry_at": to_ist(t.entry_at).strftime("%Y-%m-%d %H:%M"),
                    "exit_at": to_ist(t.exit_at).strftime("%H:%M") if t.exit_at else None,
                    "entry": t.entry,
                    "exit": t.exit,
                    "stop": round(t.stop, 2),
                    "r": round(t.r_multiple, 2),
                    "exit_reason": t.exit_reason,
                    "first_touch": t.first_touch,
                    "strike": m.strike if m else None,
                    "expiry": str(m.expiry) if m else None,
                    "prem_in": m.entry_premium if m else None,
                    "prem_out": m.exit_premium if m else None,
                    "net_1lot": round(m.net_1lot, 2) if m else None,
                    "lots_sized": m.lots_sized if m else None,
                    "real_net_1lot": round(t.real.net_1lot, 2) if t.real else None,
                }
            )
    return rows


# ------------------------------------------------------------------ HTML
_CSS = """body{font-family:system-ui,sans-serif;margin:16px;max-width:1100px;color:#1d1d1f;
background:#fff}h1{font-size:22px}h2{font-size:18px;margin-top:28px;border-bottom:1px solid #ddd}
table{border-collapse:collapse;margin:8px 0;font-size:13px;display:block;overflow-x:auto}
td,th{border:1px solid #ddd;padding:4px 8px;text-align:right}th{background:#f4f4f6}
td:first-child,th:first-child{text-align:left}.warn{background:#fff4e5;padding:8px;
border-left:4px solid #e8a33d}.tag{font-weight:600;color:#a35a00}
@media (prefers-color-scheme: dark){body{background:#151517;color:#e8e8ea}th{background:#222}
td,th{border-color:#333}.warn{background:#2a2115}}"""


def _table(rows: list[dict[str, Any]], cols: list[str] | None = None) -> str:
    if not rows:
        return "<p>none</p>"
    cols = cols or list(rows[0].keys())
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(r.get(c, '')))}</td>" for c in cols) + "</tr>"
        for r in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def _equity_svg(series: dict[str, list[float]]) -> str:
    w, h, pad = 900, 220, 30
    allv = [0.0]
    curves = {}
    for k, v in series.items():
        eq, c = [], 0.0
        for x in v:
            c += x
            eq.append(c)
        curves[k] = eq
        allv += eq
    lo, hi = min(allv), max(allv)
    span = (hi - lo) or 1.0
    n = max((len(v) for v in curves.values()), default=1) or 1
    colors = ["#2f6fdf", "#d9534f", "#2e9e5b", "#a35ad3", "#e8a33d"]
    parts = [
        f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="cumulative P&L">',
        f'<line x1="{pad}" x2="{w - pad}" y1="{h - pad - (0 - lo) / span * (h - 2 * pad):.1f}"'
        f' y2="{h - pad - (0 - lo) / span * (h - 2 * pad):.1f}" stroke="#999" '
        'stroke-dasharray="4 3"/>',
    ]
    for idx, (k, eq) in enumerate(curves.items()):
        pts = " ".join(
            f"{pad + i / max(1, n - 1) * (w - 2 * pad):.1f},"
            f"{h - pad - (y - lo) / span * (h - 2 * pad):.1f}"
            for i, y in enumerate(eq)
        )
        col = colors[idx % len(colors)]
        parts.append(f'<polyline fill="none" stroke="{col}" stroke-width="2" points="{pts}"/>')
        parts.append(
            f'<text x="{pad + 6}" y="{14 + 14 * idx}" fill="{col}" font-size="12">'
            f"{html.escape(k)}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def render_html(
    summary: dict[str, Any], results: list[BookResult], grid: dict[str, Any] | None
) -> str:
    meta = summary["meta"]
    out = [
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
        f"content='width=device-width,initial-scale=1'><title>Backtest report</title>"
        f"<style>{_CSS}</style></head><body>",
        f"<h1>Backtest {html.escape(meta['period_label'])}: {meta['from']} to {meta['to']}</h1>",
        "<p class='warn'><span class='tag'>Read first.</span> Layer 2 option P&amp;L is "
        "<b>MODELED</b> (Black-Scholes with IV from India VIX), not real prices. Layer 3 "
        "uses real recorded option candles and exists only for days the recorder ran. "
        "Past results do not show that a strategy will make money.</p>",
    ]
    out.append("<h2>Verdicts</h2>")
    rows = []
    for name, b in summary["books"].items():
        l2 = b["layer2_MODELED_option"].get("iv_x1.0", {})
        rows.append(
            {
                "book": name,
                "description": b["description"],
                "trades": b["trades"],
                "mean R": b["layer1_underlying"]["r_multiple"]["expectancy"],
                "2R before stop": b["layer1_underlying"]["first_touch_2R_before_stop"],
                "1-lot net Rs/trade (MODELED)": l2.get("one_lot_net_inr", {}).get("expectancy"),
                "90% CI": f"{l2.get('one_lot_net_inr', {}).get('ci90_low')} .. "
                f"{l2.get('one_lot_net_inr', {}).get('ci90_high')}",
                "own": b["verdict_own"],
                "vs baseline": b.get("vs_baseline", {}).get("verdict", "(baseline)"),
            }
        )
    out.append(_table(rows))
    out.append("<h2>Cumulative modeled 1-lot net P&amp;L (Rs)</h2>")
    out.append(
        _equity_svg(
            {
                r.spec.name: [t.modeled[1.0].net_1lot for t in r.trades if 1.0 in t.modeled]
                for r in results
            }
        )
    )
    for name, b in summary["books"].items():
        out.append(f"<h2>{html.escape(name)}: {html.escape(b['description'])}</h2>")
        out.append(
            f"<p>Signals {b['signals']}, trades {b['trades']}. Rejected: "
            f"{html.escape(json.dumps(b['rejected']))}</p>"
        )
        l1 = b["layer1_underlying"]
        out.append("<h3>Layer 1: underlying (points / R)</h3>")
        out.append(_table([{**{"metric": "R multiple"}, **l1["r_multiple"]}]))
        out.append(
            _table(
                [
                    {
                        k: l1[k]
                        for k in (
                            "first_touch_2R_before_stop",
                            "first_touch_stop_first",
                            "first_touch_neither",
                            "break_even_rate_for_2R",
                            "hit_1R_in_trade",
                            "hit_2R_in_trade",
                            "avg_mfe_r_fwd",
                            "avg_mae_r_fwd",
                            "avg_risk_points",
                        )
                    }
                ]
            )
        )
        out.append("<p>By hour</p>" + _table([{"hour": k, **v} for k, v in l1["by_hour"].items()]))
        out.append(
            "<p>By weekday</p>" + _table([{"day": k, **v} for k, v in l1["by_weekday"].items()])
        )
        out.append("<h3>Layer 2: MODELED option P&amp;L (Rs), IV sensitivity</h3>")
        l2rows = []
        for sc, v in b["layer2_MODELED_option"].items():
            l2rows.append(
                {
                    "IV": sc,
                    **{
                        f"1lot {k}": v["one_lot_net_inr"][k]
                        for k in (
                            "n",
                            "win_rate",
                            "expectancy",
                            "ci90_low",
                            "ci90_high",
                            "profit_factor",
                            "total",
                            "max_drawdown",
                        )
                    },
                    "avg charges": v["avg_charges_1lot_inr"],
                    "avg premium": v["avg_entry_premium"],
                    "skipped for size": v["skipped_share"],
                    "sized n": v["sized_net_inr_after_breakers"]["n"],
                    "sized total": v["sized_net_inr_after_breakers"]["total"],
                    "sized max DD": v["sized_net_inr_after_breakers"]["max_drawdown"],
                }
            )
        out.append(_table(l2rows))
        l3 = b["layer3_REAL_option"]
        out.append(
            f"<h3>Layer 3: REAL option P&amp;L</h3><p>{l3['trades_with_real_option_data']}"
            f" of {l3['of_total']} trades have recorded option candles.</p>"
        )
        if l3["trades_with_real_option_data"]:
            out.append(_table([l3["one_lot_net_inr"]]))
        if "vs_baseline" in b:
            out.append("<h3>Against the PDH/PDL baseline (same days, same costs)</h3>")
            out.append(_table([b["vs_baseline"]]))
    if grid:
        out.append("<h2>Sensitivity grid (in-sample only)</h2>")
        for name, g in grid.items():
            out.append(
                f"<p><b>{html.escape(name)}</b>: {html.escape(json.dumps(g['stability']))}"
                "</p>" + _table(g["cells"])
            )
    out.append("<h2>Assumptions and data coverage</h2>")
    out.append(f"<pre>{html.escape(json.dumps(meta, indent=1, default=str))}</pre>")
    out.append("<h2>Trades</h2>" + _table(trade_rows(results)))
    out.append(f"<p>Generated {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC.</p></body></html>")
    return "".join(out)
