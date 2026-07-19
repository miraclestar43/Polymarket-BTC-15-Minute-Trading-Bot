#!/usr/bin/env python3
"""
Corrected conclusions — three strictly-separated evaluations. DRY-RUN /
DIAGNOSTIC ONLY. No network, no orders, no strategy changes.

A. DIRECTIONAL  — predicted side vs official outcome only. No payout
   assumptions. Accuracy, balanced accuracy, LONG/SHORT accuracy, Wilson 95%
   CIs. This is the evaluation that survives the pricing bug intact.

B. RECORDED EXECUTABLE ECONOMIC — reported ONLY when a historically-valid
   executable selected-token quote exists. It does not: shadow records store a
   YES-token midpoint, never the NO ask, and no quote timestamp. Therefore this
   evaluation is UNAVAILABLE for historical data and is reported as such (never
   guessed or substituted with model probability).

C. SYNTHETIC MIDPOINT SENSITIVITY — a clearly-labelled hypothetical that prices
   the selected token at its midpoint (YES for long, 1-YES for short) under a
   fixed $1 max-loss. NOT executable P/L (no spread/slippage/fees). Provided so
   the corrected order of magnitude is visible, never presented as achieved.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from core.strategy_brain.settlement import market_identity, outcome_wins_side  # noqa: E402
from core.strategy_brain import economics as econ  # noqa: E402
from analyze_strategy_experiments import (  # noqa: E402
    load_shadow_records, load_settlements, latest_run_window, _parse_when,
)


def wilson_ci(wins: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    """Wilson score 95% interval for a binomial proportion (deterministic)."""
    if n == 0:
        return None, None
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return round(centre - half, 4), round(centre + half, 4)


def _resolved_candidates(records, settlements, variant):
    seen = set()
    out = []
    for r in records:
        if r.get("config_name") != variant or not r.get("would_trade"):
            continue
        ident = market_identity(r)
        k = ident["stable_key"]
        if k in seen:
            continue
        s = settlements.get(k)
        if not s or s.get("resolution_status") != "resolved":
            continue
        win = outcome_wins_side(s.get("resolved_outcome"), r.get("shadow_side"))
        if win is None:
            continue
        seen.add(k)
        out.append({
            "side": r.get("shadow_side"),
            "entry_price": r.get("entry_price"),
            "outcome": s.get("resolved_outcome"),
            "win": win,
            "schema_version": r.get("schema_version", 1),
            "selected_exec_price": r.get("selected_exec_price"),
            "economic_pnl_unavailable": r.get("economic_pnl_unavailable", True),
        })
    return out


def evaluate_variant(cands: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(cands)
    wins = sum(1 for c in cands if c["win"])
    longs = [c for c in cands if c["side"] == "long"]
    shorts = [c for c in cands if c["side"] == "short"]
    lw = sum(1 for c in longs if c["win"])
    sw = sum(1 for c in shorts if c["win"])
    long_acc = (lw / len(longs)) if longs else None
    short_acc = (sw / len(shorts)) if shorts else None
    bal = (((long_acc or 0) + (short_acc or 0)) / 2
           if longs and shorts else None)

    # B. executable economic availability
    exec_available = sum(
        1 for c in cands
        if not c.get("economic_pnl_unavailable", True)
        and c.get("selected_exec_price") is not None)

    # C. synthetic midpoint fixed-risk
    synth = econ.aggregate(
        [{"side": c["side"], "entry_price": c["entry_price"], "outcome": c["outcome"]}
         for c in cands],
        sizing_mode=econ.FIXED_RISK, risk=econ.DEFAULT_FIXED_RISK)

    lo, hi = wilson_ci(wins, n)
    return {
        "A_directional": {
            "n": n, "correct": wins,
            "accuracy": round(wins / n, 4) if n else None,
            "accuracy_wilson95": [lo, hi],
            "long_n": len(longs), "long_accuracy": round(long_acc, 4) if long_acc is not None else None,
            "short_n": len(shorts), "short_accuracy": round(short_acc, 4) if short_acc is not None else None,
            "balanced_accuracy": round(bal, 4) if bal is not None else None,
        },
        "B_recorded_executable_economic": {
            "status": "available" if exec_available else "unavailable",
            "records_with_executable_selected_quote": exec_available,
            "reason": (None if exec_available else
                       "historical records store a YES midpoint only; no NO ask "
                       "or quote timestamp \u2014 executable P/L not recoverable"),
        },
        "C_synthetic_midpoint_fixed_risk": {
            "note": "HYPOTHETICAL midpoint P/L, NOT executable (no spread/fees).",
            "net_pnl": synth["net_pnl"],
            "return_on_capital_at_risk_pct": synth["return_on_capital_at_risk_pct"],
            "median_return_per_trade": synth["median_return_per_trade"],
            "resolved_trades": synth["resolved_trades"],
        },
    }


def build(since, until, log_dir="logs") -> Dict[str, Any]:
    records = load_shadow_records("", log_dir=log_dir, since=since, until=until)
    settlements = load_settlements(log_dir)
    variants = sorted({r.get("config_name", "unknown") for r in records})
    out = {}
    for v in variants:
        out[v] = evaluate_variant(_resolved_candidates(records, settlements, v))
    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "scope": {"since": since, "until": until},
        "evaluations": {
            "A": "directional only (survives the pricing bug)",
            "B": "recorded executable economic (UNAVAILABLE historically)",
            "C": "synthetic midpoint fixed-risk (hypothetical, not executable)",
        },
        "variants": out,
    }


def to_markdown(report: Dict[str, Any]) -> str:
    lines = ["# Corrected Conclusions — Three Separated Evaluations", "",
             f"- Generated: {report['generated']}",
             "- A = directional (real). B = executable economic (UNAVAILABLE "
             "historically). C = synthetic midpoint (hypothetical, not executable).",
             "",
             "| variant | n | dir acc | 95% CI | long acc | short acc | bal acc | "
             "B status | C synth net (mid, $1 risk) | C ror% |",
             "|---|--:|--:|--|--:|--:|--:|--|--:|--:|"]
    for v in sorted(report["variants"]):
        d = report["variants"][v]
        a = d["A_directional"]; b = d["B_recorded_executable_economic"]; c = d["C_synthetic_midpoint_fixed_risk"]
        ci = a["accuracy_wilson95"]
        lines.append(
            f"| {v} | {a['n']} | {a['accuracy']} | "
            f"[{ci[0]}, {ci[1]}] | {a['long_accuracy']} | {a['short_accuracy']} | "
            f"{a['balanced_accuracy']} | {b['status']} | {c['net_pnl']} | "
            f"{c['return_on_capital_at_risk_pct']} |")
    lines += ["",
              "**B is UNAVAILABLE for every historical variant**: shadow records "
              "store a YES-token midpoint only (no NO ask, no quote timestamp), so "
              "executable P/L is not recoverable. Schema v2 begins capturing the "
              "data needed for a valid B on future runs.",
              "",
              "**C is a midpoint hypothetical**, not achieved P/L. Even so it "
              "collapses the previously-reported profitability (e.g. shadow_no_kelly "
              "from ~$33.7k to ~$14 at $1 risk), confirming the prior economic "
              "results were an artifact of pricing shorts against the YES token."]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Corrected three-evaluation report.")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--latest-run", action="store_true")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--out-dir", default="logs")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    since = _parse_when(args.since)
    until = _parse_when(args.until)
    if args.latest_run:
        win = latest_run_window(args.log_dir)
        if win:
            since, until = win["start_ts"], win["stop_ts"]

    report = build(since, until, log_dir=args.log_dir)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "corrected_conclusions.json").write_text(json.dumps(report, indent=2))
    (out / "corrected_conclusions.md").write_text(to_markdown(report))
    print(f"Wrote {out / 'corrected_conclusions.json'}")
    print(f"Wrote {out / 'corrected_conclusions.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
