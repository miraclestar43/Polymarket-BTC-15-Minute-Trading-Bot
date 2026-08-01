#!/usr/bin/env python3
"""Paired variant comparison + fee sensitivity + regime/day/half-window
robustness, for a forensic CSV already produced by
scripts/dual_book_run_audit.py — READ-ONLY, DIAGNOSTIC ONLY.

Clusters uncertainty by condition_id (market) so the 15 variants sharing
the same market opportunities are never treated as 15 independent samples
of the same event. Never touches any strategy/config file, and never
writes to any log or raw run directory -- input CSV path and output JSON
path are both explicit arguments.

History: an earlier ad-hoc copy of this analysis (used for the Week-1 and
Week-2 closeouts, kept only as a preserved audit artifact under
/home/star/polymarket-*-final-audit-*/) filtered the executable-fill
population with ``fill_status in ("filled", "partial")``. The actual
canonical value paper_execution.py ever writes for a partial fill is
``"partially_filled"`` (see core/strategy_brain/paper_execution.py:
FILLED / PARTIALLY_FILLED / UNAVAILABLE), so ``"partial"`` never matched
anything and every partial-fill record was silently dropped from the
fee-sensitivity and paired-comparison population. This version imports
the real constants instead of re-typing the string.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.strategy_brain.paper_execution import (  # noqa: E402
    FILLED, PARTIALLY_FILLED,
)

FEE_RATES = [0.0, 0.0025, 0.005, 0.01, 0.02]

# Fill-status values that represent an EXECUTABLE outcome (some capital was
# actually simulated as spent) -- i.e. everything except UNAVAILABLE. Kept
# as a tuple of the real constants, not re-typed string literals, so this
# can never drift from core/strategy_brain/paper_execution.py again.
EXECUTABLE_FILL_STATUSES = (FILLED, PARTIALLY_FILLED)


def f(x: Optional[str]) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_rows(csv_path: Path) -> List[Dict[str, Any]]:
    with open(csv_path) as fh:
        return list(csv.DictReader(fh))


def is_valid_executable(r: Dict[str, Any]) -> bool:
    """View-B population: real side, real selected-token ask, resolved
    outcome, and an executable fill outcome (full OR partial -- an
    unavailable/crossed-book row is directional-only, never economic)."""
    return (
        r["shadow_side"] in ("long", "short")
        and r["selected_exec_price"] not in (None, "")
        and r["resolution_status"] == "resolved"
        and r["win"] not in (None, "")
        and r["fill_status"] in EXECUTABLE_FILL_STATUSES
        and f(r["spent"]) not in (None, 0.0)
    )


def net_pnl_at_fee(r: Dict[str, Any], fee_rate: float) -> float:
    spent = f(r["spent"]) or 0.0
    win = r["win"] == "1"
    filled = f(r["filled_shares"]) or 0.0
    gross_payout = filled * 1.0 if win else 0.0
    gross = gross_payout - spent
    fees = fee_rate * spent
    return gross - fees


def wilson_ci(wins: int, n: int, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    if n == 0:
        return (None, None)
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    adj = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    lo = (centre - adj) / denom
    hi = (centre + adj) / denom
    return (round(max(0.0, lo), 4), round(min(1.0, hi), 4))


def bootstrap_ci(values, cluster_ids, n_boot=5000, seed=20260727):
    """Cluster (market-level) bootstrap of the mean; resample cluster ids
    with replacement, not individual records, so repeated markets across
    variants/cycles are never pseudo-replicated. Deterministic (fixed
    seed) unless overridden."""
    rng = random.Random(seed)
    by_cluster = defaultdict(list)
    for v, c in zip(values, cluster_ids):
        by_cluster[c].append(v)
    clusters = list(by_cluster.keys())
    if not clusters:
        return (None, None, None)
    means = []
    for _ in range(n_boot):
        sample_clusters = [rng.choice(clusters) for _ in clusters]
        vals = []
        for c in sample_clusters:
            vals.extend(by_cluster[c])
        if vals:
            means.append(sum(vals) / len(vals))
    means.sort()
    if not means:
        return (None, None, None)
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means))]
    return (round(st.mean(means), 4), round(lo, 4), round(hi, 4))


def run_analysis(
    csv_path: Path,
    run_start: datetime,
    freeze_ts: datetime,
    baseline_name: str = "baseline",
    largest_variant_name: str = "shadow_no_kelly",
) -> Dict[str, Any]:
    rows = load_rows(csv_path)
    valid = [r for r in rows if is_valid_executable(r)]
    variants = sorted(set(r["config_name"] for r in rows))
    mid = run_start + (freeze_ts - run_start) / 2

    # -----------------------------------------------------------------
    # Fee sensitivity per variant (hypothetical, not actual fees)
    # -----------------------------------------------------------------
    fee_sensitivity: Dict[str, Any] = {}
    for name in variants:
        vrows = [r for r in valid if r["config_name"] == name]
        n = len(vrows)
        entry = {}
        for fee in FEE_RATES:
            pnls = [net_pnl_at_fee(r, fee) for r in vrows]
            spent = sum(f(r["spent"]) or 0.0 for r in vrows)
            net = sum(pnls)
            entry[f"fee_{fee:.4f}"] = {
                "n": n,
                "net_pnl": round(net, 4),
                "capital_at_risk": round(spent, 4),
                "return_on_risk": round(net / spent, 4) if spent else None,
            }
        fee_sensitivity[name] = entry

    # -----------------------------------------------------------------
    # Paired comparison, baseline vs every other variant, joined on the
    # SAME market opportunity (condition_id + cycle timestamp).
    # -----------------------------------------------------------------
    by_variant_cycle: Dict[str, Dict[Any, Any]] = defaultdict(dict)
    for r in rows:
        key = (r["condition_id"], r["timestamp"])
        by_variant_cycle[r["config_name"]][key] = r

    baseline_map = by_variant_cycle.get(baseline_name, {})
    paired: Dict[str, Any] = {}
    for name in variants:
        if name == baseline_name:
            continue
        vmap = by_variant_cycle[name]
        common_keys = set(baseline_map) | set(vmap)
        added, removed, same_side, changed_side = 0, 0, 0, 0
        paired_diffs = []
        paired_cluster_ids = []
        b_wins = b_losses = v_wins = v_losses = 0
        for key in common_keys:
            b = baseline_map.get(key)
            v = vmap.get(key)
            # The forensic CSV only contains would_trade=True rows, so presence
            # in the map already implies would_trade=True for that variant at
            # that cycle; still require a valid (non-"unknown") side.
            b_trade = b is not None and b["shadow_side"] in ("long", "short")
            v_trade = v is not None and v["shadow_side"] in ("long", "short")
            if v_trade and not b_trade:
                added += 1
            elif b_trade and not v_trade:
                removed += 1
            elif b_trade and v_trade:
                if b["shadow_side"] == v["shadow_side"]:
                    same_side += 1
                else:
                    changed_side += 1
            # Incremental economic P/L only where BOTH sides are valid-executable
            b_valid = b is not None and is_valid_executable(b)
            v_valid = v is not None and is_valid_executable(v)
            b_pnl = net_pnl_at_fee(b, 0.0) if b_valid else 0.0
            v_pnl = net_pnl_at_fee(v, 0.0) if v_valid else 0.0
            if b_valid or v_valid:
                paired_diffs.append(v_pnl - b_pnl)
                paired_cluster_ids.append(key[0])  # cluster by condition_id
            if b_valid:
                if b["win"] == "1":
                    b_wins += 1
                else:
                    b_losses += 1
            if v_valid:
                if v["win"] == "1":
                    v_wins += 1
                else:
                    v_losses += 1

        mean_diff, lo, hi = bootstrap_ci(paired_diffs, paired_cluster_ids)
        paired[name] = {
            "common_market_opportunities": len(common_keys),
            "added_trades_vs_baseline": added,
            "removed_trades_vs_baseline": removed,
            "same_side_decisions": same_side,
            "changed_side_decisions": changed_side,
            "baseline_valid_wins_losses": [b_wins, b_losses],
            "variant_valid_wins_losses": [v_wins, v_losses],
            "raw_incremental_pnl_sum": round(sum(paired_diffs), 4),
            "paired_bootstrap_mean_incremental_pnl": mean_diff,
            "paired_bootstrap_95ci": [lo, hi],
            "n_paired_observations": len(paired_diffs),
            "n_clusters_condition_id": len(set(paired_cluster_ids)),
        }

    # -----------------------------------------------------------------
    # Break-even win rate conditional on entry price (per trade) and
    # calibration vs market-implied probability (entry price as proxy).
    # -----------------------------------------------------------------
    calib = []
    for r in valid:
        p_entry = f(r["selected_exec_price"])
        if p_entry is None or not (0 < p_entry < 1):
            continue
        break_even = p_entry  # buying at price p, breakeven win rate = p (binary $1 payoff)
        calib.append({
            "config": r["config_name"], "side": r["shadow_side"],
            "entry_price": p_entry, "break_even_win_rate": break_even,
            "win": r["win"] == "1",
        })

    def calib_bins(items, key="entry_price", nbins=5):
        if not items:
            return []
        items = sorted(items, key=lambda x: x[key])
        n = len(items)
        bins = []
        for i in range(nbins):
            lo_i = i * n // nbins
            hi_i = (i + 1) * n // nbins
            chunk = items[lo_i:hi_i]
            if not chunk:
                continue
            avg_p = sum(c[key] for c in chunk) / len(chunk)
            win_rate = sum(1 for c in chunk if c["win"]) / len(chunk)
            bins.append({
                "n": len(chunk),
                "avg_entry_price_or_break_even": round(avg_p, 4),
                "observed_win_rate": round(win_rate, 4),
                "range": [round(chunk[0][key], 4), round(chunk[-1][key], 4)],
            })
        return bins

    calibration = {
        baseline_name: calib_bins([c for c in calib if c["config"] == baseline_name]),
        largest_variant_name: calib_bins([c for c in calib if c["config"] == largest_variant_name]),
    }

    # -----------------------------------------------------------------
    # Regime / day / half-window robustness -- ONE variant at a time
    # (never pooled across variants, per the no-pseudo-replication rule).
    # -----------------------------------------------------------------
    def robustness_for(name):
        vrows = [r for r in valid if r["config_name"] == name]
        out: Dict[str, Any] = {}
        for side in ("long", "short"):
            sub = [r for r in vrows if r["shadow_side"] == side]
            wins = sum(1 for r in sub if r["win"] == "1")
            pnl = sum(net_pnl_at_fee(r, 0.0) for r in sub)
            spent = sum(f(r["spent"]) or 0.0 for r in sub)
            out[f"side_{side}"] = {
                "n": len(sub), "wins": wins,
                "accuracy": round(wins / len(sub), 4) if sub else None,
                "net_pnl": round(pnl, 4),
                "return_on_risk": round(pnl / spent, 4) if spent else None,
            }
        for half, pred in (("first_half", lambda ts: ts < mid), ("second_half", lambda ts: ts >= mid)):
            sub = [r for r in vrows if pred(datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")))]
            wins = sum(1 for r in sub if r["win"] == "1")
            pnl = sum(net_pnl_at_fee(r, 0.0) for r in sub)
            spent = sum(f(r["spent"]) or 0.0 for r in sub)
            out[half] = {
                "n": len(sub), "wins": wins,
                "accuracy": round(wins / len(sub), 4) if sub else None,
                "net_pnl": round(pnl, 4),
                "return_on_risk": round(pnl / spent, 4) if spent else None,
            }
        by_day = defaultdict(list)
        for r in vrows:
            d = r["timestamp"][:10]
            by_day[d].append(r)
        day_table = []
        cum = 0.0
        for d in sorted(by_day):
            sub = by_day[d]
            wins = sum(1 for r in sub if r["win"] == "1")
            pnl = sum(net_pnl_at_fee(r, 0.0) for r in sub)
            cum += pnl
            day_table.append({
                "date": d, "n": len(sub), "wins": wins,
                "accuracy": round(wins / len(sub), 4) if sub else None,
                "day_net_pnl": round(pnl, 4), "cumulative_net_pnl": round(cum, 4),
            })
        out["day_by_day"] = day_table
        pnls_sorted = sorted((net_pnl_at_fee(r, 0.0) for r in vrows), key=lambda x: -abs(x))
        total_abs = sum(abs(p) for p in pnls_sorted) or 1.0
        out["pnl_concentration"] = {
            "top1_pct_of_abs_pnl": round(abs(pnls_sorted[0]) / total_abs, 4) if pnls_sorted else None,
            "top5_pct_of_abs_pnl": round(sum(abs(p) for p in pnls_sorted[:5]) / total_abs, 4) if len(pnls_sorted) >= 1 else None,
            "top10_pct_of_abs_pnl": round(sum(abs(p) for p in pnls_sorted[:10]) / total_abs, 4) if len(pnls_sorted) >= 1 else None,
            "n": len(pnls_sorted),
        }
        return out

    robustness = {
        baseline_name: robustness_for(baseline_name),
        largest_variant_name: robustness_for(largest_variant_name),
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_start": run_start.isoformat(),
        "freeze_ts": freeze_ts.isoformat(),
        "mid_point": mid.isoformat(),
        "n_total_forensic_rows": len(rows),
        "n_valid_executable_total": len(valid),
        "fee_sensitivity_hypothetical": fee_sensitivity,
        "paired_vs_baseline": paired,
        "break_even_calibration": calibration,
        "robustness": robustness,
    }


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Paired variant comparison + fee sensitivity + robustness "
                     "for a scripts/dual_book_run_audit.py forensic CSV (read-only).")
    ap.add_argument("--csv", required=True, help="path to the forensic CSV")
    ap.add_argument("--run-start", required=True, help="ISO8601 run start")
    ap.add_argument("--freeze-ts", required=True, help="ISO8601 analysis end/freeze boundary")
    ap.add_argument("--out", required=True, help="output JSON path")
    ap.add_argument("--baseline-name", default="baseline")
    ap.add_argument("--largest-variant-name", default="shadow_no_kelly")
    args = ap.parse_args(argv)

    result = run_analysis(
        csv_path=Path(args.csv),
        run_start=_parse_iso(args.run_start),
        freeze_ts=_parse_iso(args.freeze_ts),
        baseline_name=args.baseline_name,
        largest_variant_name=args.largest_variant_name,
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=1)
    print(f"total forensic rows: {result['n_total_forensic_rows']}  "
          f"valid-executable (View B pop): {result['n_valid_executable_total']}")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
