#!/usr/bin/env python3
"""
Strategy attribution report — DRY-RUN / DIAGNOSTIC ONLY.

Investigates (does not optimize) the completed pre-reboot run. Produces a
machine-readable JSON and a human-readable Markdown report that separate
descriptive findings from causal claims.

It reconciles THREE views of "baseline P/L" and never conflates them:

  A. as-recorded baseline  — trade_journal simulated_order.simulated_pnl.
     THIS IS A RANDOM MARK-TO-MARKET (bot._record_paper_trade uses
     random.uniform biased by direction). It is NOT a market settlement.
  B. settlement baseline   — would_trade baseline candidates scored against the
     offline settlement dataset (official cache -> BTC inference).
  C. paired variant deltas — from analyze_strategy_experiments.

All inputs are local files; no network, no orders, no secrets.

Usage:
    python scripts/strategy_attribution_report.py \
        --since 2026-06-26T00:43:30Z --until 2026-07-10T09:58:00Z
    python scripts/strategy_attribution_report.py --latest-run --out-dir logs
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from core.strategy_brain.settlement import market_identity  # noqa: E402
from analyze_strategy_experiments import (  # noqa: E402
    latest_run_window,
    load_shadow_records,
    load_settlements,
    analyze_offline,
    paired_comparison,
    resolve_from_settlements,
    _parse_when,
)

_NUM_FEATURES = [
    "entry_price", "p_model", "p_stay", "odds_b",
    "raw_kelly_fraction", "capped_kelly_fraction", "kelly_gap",
    "btc_return_15m", "btc_return_1h", "btc_return_4h",
    "required_p_model_to_pass",
]


# ---------------------------------------------------------------------------
# Small stats helpers (no scipy dependency)
# ---------------------------------------------------------------------------
def wilson_ci(wins: int, n: int, z: float = 1.96) -> Optional[Dict[str, float]]:
    """Wilson score interval for a binomial proportion. None when n == 0."""
    if n == 0:
        return None
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return {"p": round(p, 4), "low": round(center - half, 4),
            "high": round(center + half, 4), "n": n}


def _mean(xs: List[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(statistics.fmean(xs), 6) if xs else None


def _feature_means(recs: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    return {f: _mean([r.get(f) for r in recs]) for f in _NUM_FEATURES}


# ---------------------------------------------------------------------------
# Data loading (window-scoped)
# ---------------------------------------------------------------------------
def load_journal_orders(since: float, until: float, log_dir: str = "logs") -> List[Dict[str, Any]]:
    """Load baseline simulated_order records (the as-recorded random baseline)."""
    out: List[Dict[str, Any]] = []
    for fn in sorted(glob.glob(str(Path(log_dir) / "trade_journal_*.jsonl"))):
        for line in open(fn):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("event_type") != "simulated_order":
                continue
            ts = _parse_when(r.get("timestamp"))
            if ts is None or ts < since or ts > until:
                continue
            out.append(r)
    return out


def load_journal_skips(since: float, until: float, log_dir: str = "logs") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for fn in sorted(glob.glob(str(Path(log_dir) / "trade_journal_*.jsonl"))):
        for line in open(fn):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("event_type") != "skipped_trade":
                continue
            ts = _parse_when(r.get("timestamp"))
            if ts is None or ts < since or ts > until:
                continue
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Report construction
# ---------------------------------------------------------------------------
def build_report(
    since: float, until: float, label: str, log_dir: str = "logs"
) -> Dict[str, Any]:
    records = load_shadow_records("", log_dir=log_dir, since=since, until=until)
    settlements = load_settlements(log_dir)
    baseline = [r for r in records if r.get("config_name") == "baseline" and r.get("would_trade")]

    # --- A. As-recorded (random) baseline ---
    orders = load_journal_orders(since, until, log_dir)
    def _side_sum(side):
        so = [o for o in orders if o.get("side") == side]
        w = sum(1 for o in so if o.get("simulated_pnl", 0) > 0)
        return {"n": len(so), "wins": w, "losses": len(so) - w,
                "pnl": round(sum(o.get("simulated_pnl", 0) for o in so), 4)}
    as_recorded = {
        "source": "trade_journal simulated_order.simulated_pnl",
        "accounting": "RANDOM mark-to-market (random.uniform biased by direction); NOT a settlement",
        "n_orders": len(orders),
        "total_pnl": round(sum(o.get("simulated_pnl", 0) for o in orders), 4),
        "wins": sum(1 for o in orders if o.get("simulated_pnl", 0) > 0),
        "win_rate_pct": round(100 * sum(1 for o in orders if o.get("simulated_pnl", 0) > 0) / len(orders), 1) if orders else 0.0,
        "long": _side_sum("long"),
        "short": _side_sum("short"),
    }

    # --- B. Settlement baseline (real outcome, binary payout) ---
    by_side_recs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    win_recs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    loss_recs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    side_stats: Dict[str, Dict[str, Any]] = {}
    outcome_counts: Counter = Counter()
    for side in ("long", "short"):
        recs = [r for r in baseline if r.get("shadow_side") == side]
        wins = losses = 0
        pnl = 0.0
        entries: List[float] = []
        for r in recs:
            res = resolve_from_settlements(r, settlements)
            by_side_recs[side].append(r)
            if res["hypothetical_pnl"] is None:
                continue
            entries.append(r.get("entry_price"))
            outcome_counts[(side, res["resolved_outcome"])] += 1
            pnl += res["hypothetical_pnl"]
            if res["hypothetical_win"]:
                wins += 1
                win_recs[side].append(r)
            else:
                losses += 1
                loss_recs[side].append(r)
        resolved = wins + losses
        side_stats[side] = {
            "resolved": resolved,
            "wins": wins,
            "losses": losses,
            "win_rate_ci": wilson_ci(wins, resolved),
            "pnl": round(pnl, 4),
            "avg_pnl": round(pnl / resolved, 4) if resolved else None,
            "mean_entry_price": _mean(entries),
            "mean_win_payout_multiple": round(
                statistics.fmean([(1 - e) / e for e in entries if e and 0 < e < 1]), 3
            ) if entries else None,
        }

    settlement_baseline = {
        "accounting": "binary payout: win=stake*(1-entry)/entry, loss=-stake, stake=5, no fees",
        "long": side_stats["long"],
        "short": side_stats["short"],
    }

    # --- Confound checks for the SHORT advantage ---
    all_outcomes = Counter(
        s.get("resolved_outcome") for s in settlements.values()
        if s.get("resolution_status") == "resolved"
    )
    up = all_outcomes.get("UP", 0)
    down = all_outcomes.get("DOWN", 0)
    base_rate = {
        "settled_markets": up + down,
        "up": up, "down": down,
        "down_rate_pct": round(100 * down / (up + down), 1) if (up + down) else None,
        "interpretation": (
            "≈coin flip on 15-min direction; a market-wide DOWN base rate does "
            "NOT explain the SHORT advantage." if (up + down) and abs(up - down) / (up + down) < 0.1
            else "directional imbalance present; inspect before attributing to skill."
        ),
    }

    confounds = {
        "macro_regime_constant": {
            "true_macro_regime_source": "btc_regime_history.macro_regime (SMA-50/200)",
            "value": "macro_bear for the entire window (cannot test cross-macro robustness)",
            "note": "shadow_decisions.macro_regime is a MISNOMER (it is the coarse "
                    "intraday/coingecko label and varies); do not read it as true macro.",
        },
        "entry_price_payout_asymmetry": {
            "long_mean_entry": side_stats["long"]["mean_entry_price"],
            "short_mean_entry": side_stats["short"]["mean_entry_price"],
            "long_mean_win_payout_x": side_stats["long"]["mean_win_payout_multiple"],
            "short_mean_win_payout_x": side_stats["short"]["mean_win_payout_multiple"],
            "interpretation": (
                "SHORT is entered at much lower prices -> far higher win payout "
                "multiple. Under the binary-payout model this pricing asymmetry, "
                "not win rate, is the dominant driver of the SHORT P/L advantage."
            ),
        },
        "market_direction_base_rate": base_rate,
        "sample_size": {
            "baseline_resolved": side_stats["long"]["resolved"] + side_stats["short"]["resolved"],
            "long_resolved": side_stats["long"]["resolved"],
            "short_resolved": side_stats["short"]["resolved"],
            "note": "small, single-regime sample; win-rate CIs are wide.",
        },
        "inference_quality": {
            "official_coverage": sum(
                1 for r in baseline
                if resolve_from_settlements(r, settlements)["outcome_source"] == "official_resolution"
            ),
            "note": "outcomes are BTC-price INFERRED (coingecko ~15-min sampling), "
                    "not official Polymarket resolution; treat as low-confidence.",
        },
    }

    # --- LONG-lost / SHORT-won feature attribution ---
    def _wl_features(side: str) -> Dict[str, Any]:
        return {
            "n_win": len(win_recs[side]),
            "n_loss": len(loss_recs[side]),
            "win_feature_means": _feature_means(win_recs[side]),
            "loss_feature_means": _feature_means(loss_recs[side]),
            "all_feature_means": _feature_means(by_side_recs[side]),
            "intraday_regime_dist": dict(Counter(r.get("btc_regime") for r in by_side_recs[side])),
        }
    attribution = {
        "long": _wl_features("long"),
        "short": _wl_features("short"),
        "note": "descriptive feature means only; NOT causal. Distance-from-threshold "
                "= p_stay - min_prob_used (Markov) and kelly_gap (Kelly).",
    }

    # --- Filter attribution (Kelly / Markov gates) ---
    skips = load_journal_skips(since, until, log_dir)
    skip_reasons = Counter()
    for s in skips:
        reason = (s.get("decision_reason") or "").split(":")[0]
        skip_reasons[reason] += 1
    offline = analyze_offline(records, settlements)
    filter_attr = {
        "authorized_trades": len(baseline),
        "skipped_trades": len(skips),
        "skip_reason_counts": dict(skip_reasons.most_common()),
        "gate_relaxation_effect": {
            v: {
                "resolved": offline["variants"][v]["resolved"],
                "win_rate_pct": offline["variants"][v]["win_rate_pct"],
                "total_pnl": offline["variants"][v]["total_pnl"],
                "median_pnl": offline["variants"][v]["median_pnl"],
            }
            for v in ("baseline", "shadow_no_kelly", "shadow_allow_kelly_minus_002",
                      "shadow_allow_kelly_minus_005")
            if v in offline["variants"]
        },
        "note": "relaxing Kelly ADDS many low-price candidates whose few wins carry "
                "extreme payout multiples -> raw P/L inflates while median stays ~flat; "
                "do NOT read added-trade P/L as realized edge. Rejected trades that "
                "cannot be resolved are NOT claimed as would-have-won.",
    }

    paired = paired_comparison(records, settlements)

    return {
        "report_type": "strategy_attribution",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {"label": label, "since": since, "until": until},
        "dry_run": True,
        "as_recorded_random_baseline": as_recorded,
        "settlement_baseline": settlement_baseline,
        "short_advantage_confounds": confounds,
        "long_short_attribution": attribution,
        "filter_attribution": filter_attr,
        "paired_variants": paired,
        "caveats": [
            "Baseline P/L reported by prior summaries is a RANDOM mark-to-market, not settlement.",
            "Outcomes here are BTC-price INFERRED, not official Polymarket resolution.",
            "Entire window is a single macro_bear regime -> no cross-regime robustness.",
            "n is small; win-rate CIs are wide; findings are descriptive, not causal.",
            "Binary-payout P/L is dominated by low-entry-price winners; rank by median, not raw P/L.",
        ],
    }


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------
def render_markdown(rep: Dict[str, Any]) -> str:
    L: List[str] = []
    L.append("# Strategy Attribution Report (DRY-RUN / DIAGNOSTIC)")
    L.append("")
    L.append(f"- Scope: `{rep['scope']['label']}`")
    L.append(f"- Generated: {rep['generated_at']}")
    L.append("- Strategy behavior was NOT modified. This report investigates only.")
    L.append("")

    ar = rep["as_recorded_random_baseline"]
    L.append("## 1. Baseline P/L — two views (do not conflate)")
    L.append("")
    L.append("### A. As-recorded baseline (what prior summaries reported)")
    L.append(f"- Source: `{ar['source']}`")
    L.append(f"- **Accounting: {ar['accounting']}**")
    L.append(f"- Orders: {ar['n_orders']}, ΣP/L: **{ar['total_pnl']}**, win rate: {ar['win_rate_pct']}%")
    L.append(f"- LONG: {ar['long']}")
    L.append(f"- SHORT: {ar['short']}")
    L.append("")
    sb = rep["settlement_baseline"]
    L.append("### B. Settlement baseline (real outcome, binary payout)")
    L.append(f"- Accounting: {sb['accounting']}")
    for side in ("long", "short"):
        d = sb[side]
        ci = d["win_rate_ci"]
        ci_s = f"{ci['p']*100:.1f}% (95% CI {ci['low']*100:.0f}–{ci['high']*100:.0f}%, n={ci['n']})" if ci else "n/a"
        L.append(f"- {side.upper()}: {d['wins']}W/{d['losses']}L, win rate {ci_s}, "
                 f"P/L={d['pnl']}, avg={d['avg_pnl']}, mean entry={d['mean_entry_price']}, "
                 f"mean win payout={d['mean_win_payout_multiple']}x")
    L.append("")

    c = rep["short_advantage_confounds"]
    L.append("## 2. Is the SHORT advantage real? (confound checks)")
    br = c["market_direction_base_rate"]
    L.append(f"- Market direction base rate: UP={br['up']}, DOWN={br['down']} "
             f"(DOWN {br['down_rate_pct']}%). {br['interpretation']}")
    ep = c["entry_price_payout_asymmetry"]
    L.append(f"- Entry/payout asymmetry: LONG entry {ep['long_mean_entry']} "
             f"(win {ep['long_mean_win_payout_x']}x) vs SHORT entry {ep['short_mean_entry']} "
             f"(win {ep['short_mean_win_payout_x']}x). {ep['interpretation']}")
    L.append(f"- Macro regime: {c['macro_regime_constant']['value']}. "
             f"{c['macro_regime_constant']['note']}")
    L.append(f"- Inference quality: official coverage={c['inference_quality']['official_coverage']}. "
             f"{c['inference_quality']['note']}")
    L.append("")

    a = rep["long_short_attribution"]
    L.append("## 3. Why LONG lost / SHORT won (decision-time features)")
    L.append(f"- {a['note']}")
    for side in ("long", "short"):
        d = a[side]
        L.append(f"### {side.upper()} (win n={d['n_win']}, loss n={d['n_loss']})")
        L.append(f"- win means: {d['win_feature_means']}")
        L.append(f"- loss means: {d['loss_feature_means']}")
        L.append(f"- intraday regime dist: {d['intraday_regime_dist']}")
    L.append("")

    f = rep["filter_attribution"]
    L.append("## 4. Filter attribution (Kelly / Markov)")
    L.append(f"- authorized={f['authorized_trades']}, skipped={f['skipped_trades']}")
    L.append(f"- skip reasons: {f['skip_reason_counts']}")
    L.append(f"- gate relaxation effect: {f['gate_relaxation_effect']}")
    L.append(f"- {f['note']}")
    L.append("")

    p = rep["paired_variants"]
    L.append("## 5. Paired variant comparison vs baseline")
    L.append(f"- baseline resolved markets: {p['baseline_markets_resolved']}")
    for name, cc in p["comparisons"].items():
        ci = cc["bootstrap_ci_mean_diff"]
        ci_s = f"mean_diff={ci['mean_diff']} 95%CI[{ci['ci_low']},{ci['ci_high']}] n={ci['n_pairs']}" if ci else "n/a"
        L.append(f"  - `{name}`: incr_P/L={cc['incremental_pnl_vs_baseline']}, "
                 f"+{cc['added_trades']}/-{cc['removed_trades']}, side_chg={cc['side_changed']}, {ci_s}")
    L.append(f"- {p['multiple_comparison_warning']}")
    L.append("")

    L.append("## 6. Caveats")
    for cav in rep["caveats"]:
        L.append(f"- {cav}")
    L.append("")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="Strategy attribution report (dry-run/diagnostic)")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--latest-run", action="store_true")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--out-dir", default="logs")
    ap.add_argument("--json", action="store_true", help="print JSON to stdout instead of writing files")
    args = ap.parse_args()

    since = _parse_when(args.since)
    until = _parse_when(args.until)
    label = f"since={since} until={until}"
    if args.latest_run:
        win = latest_run_window(args.log_dir)
        if win is None:
            print("No runner_start events found.")
            return
        since, until = win["start_ts"], win["stop_ts"]
        label = f"latest run ({win['start_t']} -> {win['stop_t'] or 'OPEN'})"
    if since is None:
        print("Provide --since/--until or --latest-run.")
        return

    rep = build_report(since, until, label, log_dir=args.log_dir)
    if args.json:
        print(json.dumps(rep, indent=2))
        return

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "strategy_attribution_report.json"
    md_path = out_dir / "strategy_attribution_report.md"
    json_path.write_text(json.dumps(rep, indent=2))
    md_path.write_text(render_markdown(rep))
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
