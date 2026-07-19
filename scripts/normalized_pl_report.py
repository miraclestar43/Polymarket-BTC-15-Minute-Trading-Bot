#!/usr/bin/env python3
"""
Normalized economic P/L report — DRY-RUN / DIAGNOSTIC ONLY.

Re-states hypothetical shadow-variant results under economically valid,
low-price-artifact-resistant accounting. Produces THREE clearly-separated
views and never ranks strategies by raw summed P/L:

  1. Recorded-sizing view   — fixed $5 stake (== max loss), the historical
                              settlement semantic. Depends on that semantic.
  2. Fixed-risk view        — $1 max loss per trade (recommended fair basis).
  3. Equal-shares view      — equal FACE notional (unequal risk); shown only
                              for contrast, never used for ranking.

For every variant/side it reports capital at risk, gross payout, net P/L,
return on capital at risk, avg/median return, max loss/gain, max drawdown,
exposure-weighted win rate, entry-price distribution, low-price concentration,
top-1/5/10 P/L concentration, and an entry-price-floor sensitivity table.
Official and inferred outcomes are counted separately and never mixed silently.

Usage:
  python scripts/normalized_pl_report.py --since ISO --until ISO [--out-dir logs]
  python scripts/normalized_pl_report.py --latest-run [--json]

Never places orders. Never prints secrets. Analysis only — imposes no price
floor on the strategy.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.strategy_brain.settlement import market_identity  # noqa: E402
from core.strategy_brain.market_resolver import (  # noqa: E402
    OUTCOME_OFFICIAL, OUTCOME_INFERRED, OUTCOME_UNAVAILABLE,
)
from core.strategy_brain import economics as econ  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from analyze_strategy_experiments import (  # noqa: E402
    load_shadow_records, load_settlements, latest_run_window, _parse_when,
    _bootstrap_paired_ci,
)

_SMALL_SAMPLE = 30


def _record_ts(rec: Dict[str, Any]) -> Optional[float]:
    t = rec.get("timestamp")
    if t is None:
        return None
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def candidate_trades(
    records: List[Dict[str, Any]],
    settlements: Dict[str, Dict[str, Any]],
    variant: str,
) -> List[Dict[str, Any]]:
    """
    Build one trade dict per RESOLVED would_trade candidate of a variant.
    Deduplicates by stable market key so a decision contributes at most one
    settlement. Returns dicts with side/entry_price/outcome/source/ts/stable_key.
    """
    raw: List[Dict[str, Any]] = []
    for r in records:
        if r.get("config_name") != variant or not r.get("would_trade"):
            continue
        ident = market_identity(r)
        s = settlements.get(ident["stable_key"])
        if not s or s.get("resolution_status") != "resolved":
            continue
        raw.append({
            "stable_key": ident["stable_key"],
            "side": r.get("shadow_side", "unknown"),
            "entry_price": r.get("entry_price"),
            "outcome": s.get("resolved_outcome"),
            "source": s.get("resolution_source", OUTCOME_UNAVAILABLE),
            "ts": _record_ts(r),
        })
    deduped, _dropped = econ.dedupe_trades(raw, key_field="stable_key")
    return deduped


def _views_for(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "recorded_stake": econ.aggregate(
            trades, sizing_mode=econ.RECORDED_STAKE, stake=econ.DEFAULT_RECORDED_STAKE),
        "fixed_risk": econ.aggregate(
            trades, sizing_mode=econ.FIXED_RISK, risk=econ.DEFAULT_FIXED_RISK),
        "equal_shares": econ.aggregate(
            trades, sizing_mode=econ.EQUAL_SHARES, shares=econ.DEFAULT_EQUAL_SHARES),
    }


def build_report(
    since: Optional[float], until: Optional[float], label: str,
    log_dir: str = "logs", baseline_name: str = "baseline",
) -> Dict[str, Any]:
    records = load_shadow_records("", log_dir=log_dir, since=since, until=until)
    settlements = load_settlements(log_dir)
    variants = sorted({r.get("config_name", "unknown") for r in records})

    per_variant: Dict[str, Any] = {}
    variant_fixed_risk_by_market: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for v in variants:
        trades = candidate_trades(records, settlements, v)
        longs = [t for t in trades if t["side"] == "long"]
        shorts = [t for t in trades if t["side"] == "short"]
        src_counts = {
            OUTCOME_OFFICIAL: sum(1 for t in trades if t["source"] == OUTCOME_OFFICIAL),
            OUTCOME_INFERRED: sum(1 for t in trades if t["source"] == OUTCOME_INFERRED),
        }
        dup = econ.detect_duplicates([t["stable_key"] for t in trades])
        per_variant[v] = {
            "resolved_trades": len(trades),
            "outcome_source_counts": src_counts,
            "duplicate_keys": dup,
            "preliminary": len(trades) < _SMALL_SAMPLE,
            "all": _views_for(trades),
            "long": _views_for(longs),
            "short": _views_for(shorts),
            "price_floor_sensitivity_fixed_risk": econ.price_floor_sensitivity(
                trades, sizing_mode=econ.FIXED_RISK, risk=econ.DEFAULT_FIXED_RISK),
        }
        # per-market fixed-risk net P/L for paired comparison
        by_mkt: Dict[str, Dict[str, Any]] = {}
        for t in trades:
            s = econ.settle_trade(t["side"], t["entry_price"], t["outcome"],
                                  sizing_mode=econ.FIXED_RISK, risk=econ.DEFAULT_FIXED_RISK)
            if s["valid"]:
                by_mkt[t["stable_key"]] = {"pnl": s["net_pnl"], "side": t["side"]}
        variant_fixed_risk_by_market[v] = by_mkt

    paired = _paired(variant_fixed_risk_by_market, baseline_name)

    return {
        "scope": {"label": label, "since": since, "until": until},
        "generated": datetime.now(timezone.utc).isoformat(),
        "accounting_note": (
            "Binary payout. capital_at_risk == max_loss. Recorded-sizing uses "
            "$5 fixed stake (historical semantic); fixed-risk uses $1 max loss "
            "per trade (recommended). Equal-shares uses 1 contract (unequal "
            "risk; not for ranking). Never ranked by raw summed P/L."),
        "baseline": baseline_name,
        "variants": per_variant,
        "paired_fixed_risk_vs_baseline": paired,
    }


def _paired(
    by_market: Dict[str, Dict[str, Dict[str, Any]]], baseline_name: str
) -> Dict[str, Any]:
    base = by_market.get(baseline_name, {})
    others = sorted(set(by_market) - {baseline_name})
    comparisons: Dict[str, Any] = {}
    for name in others:
        var = by_market[name]
        keys = set(base) | set(var)
        diffs: List[float] = []
        added = removed = side_changed = 0
        incr = 0.0
        for k in keys:
            b = base.get(k)
            v = var.get(k)
            bp = b["pnl"] if b else 0.0
            vp = v["pnl"] if v else 0.0
            diffs.append(vp - bp)
            incr += vp - bp
            if b and not v:
                removed += 1
            elif v and not b:
                added += 1
            elif b and v and b["side"] != v["side"]:
                side_changed += 1
        ci = _bootstrap_paired_ci(diffs)
        comparisons[name] = {
            "union_markets": len(keys),
            "incremental_fixed_risk_pnl": round(incr, 6),
            "added_trades": added,
            "removed_trades": removed,
            "side_changed": side_changed,
            "bootstrap_ci_mean_diff": ci,
            "small_sample_warning": len(diffs) < _SMALL_SAMPLE,
        }
    return {
        "baseline_markets": len(base),
        "risk_unit": econ.DEFAULT_FIXED_RISK,
        "comparisons": comparisons,
        "multiple_comparison_warning": (
            f"{len(others)} variants compared; treat any single 'significant' "
            "CI with caution (multiple-comparison risk)."),
    }


def render_markdown(r: Dict[str, Any]) -> str:
    L: List[str] = []
    sc = r["scope"]
    L.append("# Normalized Economic P/L Report (DRY-RUN / DIAGNOSTIC)")
    L.append("")
    L.append(f"- Scope: `{sc['label']}` since={sc['since']} until={sc['until']}")
    L.append(f"- Generated: {r['generated']}")
    L.append(f"- {r['accounting_note']}")
    L.append("")
    L.append("## Per-variant views (all sides)")
    L.append("")
    L.append("| variant | res | off/inf | recorded ΣP/L | recorded ROC% | "
             "fixed-risk ΣP/L | fixed-risk ROC% | med ret | top1/5/10 share |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|--|")
    for name, d in r["variants"].items():
        rec = d["all"]["recorded_stake"]
        fr = d["all"]["fixed_risk"]
        src = d["outcome_source_counts"]
        conc = fr["concentration"]
        share = (f"{conc.get('top_1_share')}/{conc.get('top_5_share')}/"
                 f"{conc.get('top_10_share')}")
        prelim = " *PRELIM*" if d["preliminary"] and d["resolved_trades"] else ""
        L.append(
            f"| {name}{prelim} | {d['resolved_trades']} | "
            f"{src[OUTCOME_OFFICIAL]}/{src[OUTCOME_INFERRED]} | "
            f"{rec['net_pnl']} | {rec['return_on_capital_at_risk_pct']} | "
            f"{fr['net_pnl']} | {fr['return_on_capital_at_risk_pct']} | "
            f"{fr['median_return_per_trade']} | {share} |")
    L.append("")
    L.append("Ranking note: use return on capital at risk + concentration, NOT "
             "raw ΣP/L. A high top-1/5/10 share means the result depends on a "
             "few low-price longshots.")
    L.append("")

    L.append("## LONG vs SHORT (fixed-risk $1)")
    L.append("")
    for name, d in r["variants"].items():
        lo = d["long"]["fixed_risk"]
        sh = d["short"]["fixed_risk"]
        L.append(f"### {name}")
        for tag, s in (("LONG", lo), ("SHORT", sh)):
            epd = s["entry_price_distribution"]
            L.append(
                f"- {tag}: n={s['resolved_trades']} WR={s['win_rate_pct']}% "
                f"(exp-wtd {s['exposure_weighted_win_rate_pct']}%) "
                f"ΣP/L={s['net_pnl']} ROC={s['return_on_capital_at_risk_pct']}% "
                f"medRet={s['median_return_per_trade']} maxDD={s['max_drawdown']} "
                f"entry[min/med/max]={epd['min']}/{epd['median']}/{epd['max']} "
                f"low-price{epd['buckets']}")
        L.append("")

    L.append("## Entry-price-floor sensitivity (fixed-risk; ANALYSIS ONLY, not "
             "applied to the bot)")
    L.append("")
    for name, d in r["variants"].items():
        rows = d["price_floor_sensitivity_fixed_risk"]
        L.append(f"### {name}")
        L.append("| floor | trades | ΣP/L | ROC% | med ret | WR% |")
        L.append("|--:|--:|--:|--:|--:|--:|")
        for row in rows:
            L.append(
                f"| {row['price_floor']} | {row['resolved_trades']} | "
                f"{row['net_pnl']} | {row['return_on_capital_at_risk_pct']} | "
                f"{row['median_return_per_trade']} | {row['win_rate_pct']} |")
        L.append("")

    p = r["paired_fixed_risk_vs_baseline"]
    L.append("## Paired fixed-risk comparison vs baseline")
    L.append("")
    L.append(f"- baseline markets: {p['baseline_markets']} | risk unit ${p['risk_unit']}")
    for name, c in p["comparisons"].items():
        ci = c["bootstrap_ci_mean_diff"]
        ci_s = (f"mean_diff={ci['mean_diff']} 95%CI[{ci['ci_low']},{ci['ci_high']}] "
                f"n={ci['n_pairs']}" if ci else "n/a")
        warn = " ⚠ small-sample" if c["small_sample_warning"] else ""
        L.append(f"- `{name}`: incr_fixed_risk_P/L={c['incremental_fixed_risk_pnl']} "
                 f"+{c['added_trades']}/-{c['removed_trades']} "
                 f"side_chg={c['side_changed']} {ci_s}{warn}")
    L.append(f"- {p['multiple_comparison_warning']}")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Normalized economic P/L report (diagnostic).")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--latest-run", action="store_true")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    since = _parse_when(args.since)
    until = _parse_when(args.until)
    label = f"custom since={since} until={until}"
    if args.latest_run:
        win = latest_run_window(args.log_dir)
        if win:
            since, until = win["start_ts"], win["stop_ts"]
            label = f"latest run ({win['start_t']} -> {win['stop_t'] or 'OPEN'})"

    report = build_report(since, until, label, log_dir=args.log_dir)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_markdown(report))

    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "normalized_pl_report.json").write_text(json.dumps(report, indent=2))
        (out / "normalized_pl_report.md").write_text(render_markdown(report))
        print(f"\nWrote {out/'normalized_pl_report.json'}")
        print(f"Wrote {out/'normalized_pl_report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
