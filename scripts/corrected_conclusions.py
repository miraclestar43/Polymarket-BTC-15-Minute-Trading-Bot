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
            "selected_exec_price_source": r.get("selected_exec_price_source"),
            "economic_pnl_unavailable": r.get("economic_pnl_unavailable", True),
            "yes_bid": r.get("yes_bid"),
            "yes_ask": r.get("yes_ask"),
            "quote_timestamp": r.get("quote_timestamp"),
            "decision_timestamp": r.get("timestamp"),
        })
    return out


def _dist(values):
    """Compact min/median/max summary of a numeric list (or None)."""
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    vals.sort()
    return {
        "n": len(vals),
        "min": round(vals[0], 4),
        "median": round(vals[len(vals) // 2], 4),
        "max": round(vals[-1], 4),
    }


def _executable_economic(cands: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    View B: executable economic performance computed ONLY from records that
    carry a valid contemporaneous selected-token executable ask. Records marked
    ``economic_pnl_unavailable`` (e.g. shorts with no captured NO ask) are
    excluded, never priced off a midpoint or model probability.

    Sizing is fixed-risk ($1 max loss). The executable price used is the
    recorded ask actually payable for the selected token, so results already
    include the half-spread; NO additional taker fee or slippage is modelled,
    making these an optimistic upper bound on realised fills.
    """
    usable = [
        c for c in cands
        if not c.get("economic_pnl_unavailable", True)
        and c.get("selected_exec_price") is not None
    ]
    if not usable:
        return {
            "status": "unavailable",
            "records_with_executable_selected_quote": 0,
            "reason": ("no record carried a valid contemporaneous selected-token "
                       "executable ask (e.g. shorts with no captured NO ask); "
                       "executable P/L not computed \u2014 never substituted"),
        }

    settled = []
    for c in usable:
        res = econ.settle_trade(
            c["side"], None, c["outcome"],
            sizing_mode=econ.FIXED_RISK, risk=econ.DEFAULT_FIXED_RISK,
            selected_exec_price=c["selected_exec_price"])
        if res.get("valid"):
            settled.append((c, res))

    n = len(settled)
    net = sum(r["net_pnl"] for _, r in settled)
    risk_total = sum(r["capital_at_risk"] for _, r in settled)
    longs = [(c, r) for c, r in settled if c["side"] == "long"]
    shorts = [(c, r) for c, r in settled if c["side"] == "short"]
    spreads = [
        (c["yes_ask"] - c["yes_bid"])
        for c, _ in settled
        if c.get("yes_ask") is not None and c.get("yes_bid") is not None
    ]
    quote_ages = []
    for c, _ in settled:
        dt, qt = _parse_when(c.get("decision_timestamp")), _parse_when(c.get("quote_timestamp"))
        if dt is not None and qt is not None:
            quote_ages.append(dt - qt)

    def _grp(g):
        if not g:
            return None
        gn = len(g)
        gnet = sum(r["net_pnl"] for _, r in g)
        grisk = sum(r["capital_at_risk"] for _, r in g)
        return {
            "n": gn,
            "net_pnl": round(gnet, 6),
            "return_on_risk_pct": round(100 * gnet / grisk, 4) if grisk else None,
        }

    return {
        "status": "available",
        "records_with_executable_selected_quote": n,
        "sizing": "fixed_risk $1 max loss",
        "fee_slippage_assumption": (
            "buys at recorded ask (half-spread included); no extra taker fee or "
            "slippage modelled \u2014 optimistic upper bound"),
        "net_pnl": round(net, 6),
        "return_on_risk_pct": round(100 * net / risk_total, 4) if risk_total else None,
        "long": _grp(longs),
        "short": _grp(shorts),
        "executable_entry_price_dist": _dist([c["selected_exec_price"] for c, _ in settled]),
        "yes_spread_dist": _dist(spreads),
        "quote_age_seconds_dist": _dist(quote_ages),
    }


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

    # B. executable economic performance (only from valid executable asks)
    b_view = _executable_economic(cands)

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
        "B_recorded_executable_economic": b_view,
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
            "B": "recorded executable economic (only from a valid selected-token ask)",
            "C": "synthetic midpoint fixed-risk (hypothetical, not executable)",
        },
        "variants": out,
    }


def to_markdown(report: Dict[str, Any]) -> str:
    lines = ["# Corrected Conclusions — Three Separated Evaluations", "",
             f"- Generated: {report['generated']}",
             "- A = directional (real). B = executable economic (available only "
             "from a valid selected-token ask). C = synthetic midpoint "
             "(hypothetical, not executable).",
             "",
             "| variant | n | dir acc | 95% CI | long acc | short acc | bal acc | "
             "B status | B exec net ($1 risk) | B ror% | C synth net | C ror% |",
             "|---|--:|--:|--|--:|--:|--:|--|--:|--:|--:|--:|"]
    for v in sorted(report["variants"]):
        d = report["variants"][v]
        a = d["A_directional"]; b = d["B_recorded_executable_economic"]; c = d["C_synthetic_midpoint_fixed_risk"]
        ci = a["accuracy_wilson95"]
        b_net = b.get("net_pnl") if b.get("status") == "available" else None
        b_ror = b.get("return_on_risk_pct") if b.get("status") == "available" else None
        lines.append(
            f"| {v} | {a['n']} | {a['accuracy']} | "
            f"[{ci[0]}, {ci[1]}] | {a['long_accuracy']} | {a['short_accuracy']} | "
            f"{a['balanced_accuracy']} | {b['status']} | {b_net} | {b_ror} | "
            f"{c['net_pnl']} | {c['return_on_capital_at_risk_pct']} |")
    lines += ["",
              "**View B (executable economic)** is computed ONLY from records that "
              "carry a valid contemporaneous selected-token executable ask. With "
              "schema v2, LONG decisions record the executable YES ask so B is "
              "available for them; SHORT decisions remain UNAVAILABLE while the NO "
              "book is not subscribed (no NO ask captured) and are never priced off "
              "the YES token. Historical (schema v1) records have no executable ask "
              "at all and are always unavailable.",
              "",
              "B uses fixed-risk ($1 max loss) at the recorded ask (half-spread "
              "included); no extra taker fee or slippage is modelled, so B is an "
              "optimistic upper bound on realised fills \u2014 not achieved P/L.",
              "",
              "**C is a midpoint hypothetical**, not achieved P/L. It collapses the "
              "previously-reported profitability (e.g. shadow_no_kelly from ~$33.7k "
              "to ~$14 at $1 risk), confirming the prior economic results were an "
              "artifact of pricing shorts against the YES token."]
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
