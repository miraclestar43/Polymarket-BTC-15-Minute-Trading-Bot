#!/usr/bin/env python3
"""
Comprehensive Diagnostic Analysis

Analyzes actual trades, shadow decisions, p_model calibration,
fee-aware EV, BTC regime performance, and direction analysis.

Usage:
    python scripts/analyze_diagnostics.py [YYYY-MM-DD]
    python scripts/analyze_diagnostics.py --all
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timezone


def load_journal_records(date_str: str = "", log_dir: str = "logs") -> list:
    records = []
    log_path = Path(log_dir)
    if date_str:
        files = [log_path / f"trade_journal_{date_str}.jsonl"]
    else:
        files = sorted(log_path.glob("trade_journal_*.jsonl"))
    for f in files:
        if f.exists():
            with open(f) as fh:
                for line in fh:
                    if line.strip():
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
    return records


def load_shadow_records(date_str: str = "", log_dir: str = "logs") -> list:
    records = []
    log_path = Path(log_dir)
    if date_str:
        files = [log_path / f"shadow_decisions_{date_str}.jsonl"]
    else:
        files = sorted(log_path.glob("shadow_decisions_*.jsonl"))
    for f in files:
        if f.exists():
            with open(f) as fh:
                for line in fh:
                    if line.strip():
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
    return records


# ---------------------------------------------------------------------------
# p_model calibration
# ---------------------------------------------------------------------------
def analyze_p_model_calibration(records: list) -> dict:
    sims = [r for r in records if r.get("event_type") == "simulated_order"]
    if not sims:
        return {"error": "No simulated orders"}

    buckets = {
        "0.60-0.65": (0.60, 0.65), "0.65-0.70": (0.65, 0.70),
        "0.70-0.75": (0.70, 0.75), "0.75-0.80": (0.75, 0.80),
        "0.80-0.85": (0.80, 0.85), "0.85+": (0.85, 1.01),
    }
    data = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "long": 0, "short": 0})
    for s in sims:
        pm = s.get("p_model", 0)
        if pm is None: continue
        for bn, (lo, hi) in buckets.items():
            if lo <= pm < hi:
                data[bn]["trades"] += 1
                pnl = s.get("simulated_pnl", 0)
                data[bn]["pnl"] += pnl
                if pnl > 0: data[bn]["wins"] += 1
                if s.get("side") == "long": data[bn]["long"] += 1
                else: data[bn]["short"] += 1
                break

    results = {}
    for bn in sorted(buckets.keys()):
        d = data[bn]
        if d["trades"] > 0:
            wr = d["wins"] / d["trades"] * 100
            lo, hi = buckets[bn]
            results[bn] = {
                "trades": d["trades"], "wins": d["wins"],
                "win_rate": f"{wr:.0f}%", "total_pnl": round(d["pnl"], 4),
                "avg_pnl": round(d["pnl"]/d["trades"], 4),
                "long": d["long"], "short": d["short"],
                "mid_p_model": (lo+hi)/2,
                "calibration_gap": round((lo+hi)/2 - wr/100, 4),
            }
    return results


# ---------------------------------------------------------------------------
# Direction performance
# ---------------------------------------------------------------------------
def analyze_direction(journal: list) -> dict:
    sims = [r for r in journal if r.get("event_type") == "simulated_order"]
    if not sims: return {"error": "No simulated orders"}
    by_dir = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for s in sims:
        side = s.get("side", "unknown")
        pnl = s.get("simulated_pnl", 0)
        by_dir[side]["trades"] += 1
        by_dir[side]["pnl"] += pnl
        if pnl > 0: by_dir[side]["wins"] += 1
    results = {}
    for d, data in by_dir.items():
        wr = data["wins"]/data["trades"]*100 if data["trades"] > 0 else 0
        results[d] = {
            "trades": data["trades"], "wins": data["wins"],
            "win_rate": f"{wr:.0f}%", "total_pnl": round(data["pnl"], 4),
            "avg_pnl": round(data["pnl"]/data["trades"], 4) if data["trades"] > 0 else 0,
        }
    return results


# ---------------------------------------------------------------------------
# BTC regime analysis
# ---------------------------------------------------------------------------
def analyze_regime(shadow: list) -> dict:
    """Analyze LONG vs SHORT performance by BTC regime from shadow records."""
    regime_data = defaultdict(lambda: defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0}))

    # Only look at authorized trade shadow records with regime data
    for r in shadow:
        if r.get("actual_event_type") != "decision":
            continue
        if not r.get("would_trade"):
            continue
        regime = r.get("btc_regime", "unavailable")
        side = r.get("shadow_side", "unknown")
        if side in ("blocked_by_direction", "unknown"):
            continue
        regime_data[regime][side]["trades"] += 1

    # Also analyze from actual journal records
    journal_sims = []
    results = {}

    for regime, sides in regime_data.items():
        results[regime] = {}
        for side, data in sides.items():
            results[regime][side] = {
                "shadow_would_trade": data["trades"],
            }

    return results


def analyze_regime_from_shadow_all(shadow: list) -> dict:
    """Analyze regime distribution across all shadow records."""
    regime_counts = Counter(r.get("btc_regime", "unavailable") for r in shadow)
    btc_prices = [r.get("btc_price") for r in shadow if r.get("btc_price") is not None]

    return {
        "regime_distribution": dict(regime_counts.most_common()),
        "btc_price_range": {
            "min": min(btc_prices) if btc_prices else None,
            "max": max(btc_prices) if btc_prices else None,
            "count": len(btc_prices),
        },
    }


# ---------------------------------------------------------------------------
# Entry price buckets
# ---------------------------------------------------------------------------
def analyze_entry_prices(journal: list) -> dict:
    sims = [r for r in journal if r.get("event_type") == "simulated_order"]
    if not sims: return {"error": "No simulated orders"}
    buckets = {
        "0.20-0.30": (0.20, 0.30), "0.30-0.40": (0.30, 0.40),
        "0.40-0.50": (0.40, 0.50), "0.50-0.60": (0.50, 0.60),
        "0.60-0.70": (0.60, 0.70), "0.70-0.80": (0.70, 0.80),
    }
    data = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for s in sims:
        ep = s.get("entry_price", 0)
        for bn, (lo, hi) in buckets.items():
            if lo <= ep < hi:
                data[bn]["trades"] += 1
                pnl = s.get("simulated_pnl", 0)
                data[bn]["pnl"] += pnl
                if pnl > 0: data[bn]["wins"] += 1
                break
    results = {}
    for bn in sorted(buckets.keys()):
        d = data[bn]
        if d["trades"] > 0:
            wr = d["wins"]/d["trades"]*100
            results[bn] = {"trades": d["trades"], "wins": d["wins"],
                           "win_rate": f"{wr:.0f}%", "total_pnl": round(d["pnl"], 4)}
    return results


# ---------------------------------------------------------------------------
# Shadow analysis
# ---------------------------------------------------------------------------
def analyze_shadow(shadow: list) -> dict:
    if not shadow: return {"error": "No shadow records"}
    by_config = defaultdict(list)
    for r in shadow: by_config[r.get("config_name", "unknown")].append(r)
    results = {}
    for cn, recs in by_config.items():
        wt = [r for r in recs if r.get("would_trade")]
        wnt = [r for r in recs if not r.get("would_trade")]
        skip = Counter()
        for r in wnt:
            reason = r.get("shadow_skip_reason", "unknown")
            if "kelly" in reason.lower(): skip["kelly_no_edge"] += 1
            elif "markov" in reason.lower(): skip["markov_below_threshold"] += 1
            elif "direction" in reason.lower(): skip["direction_blocked"] += 1
            elif "liquidity" in reason.lower(): skip["liquidity_guard"] += 1
            elif "fee" in reason.lower(): skip["fee_aware_ev_negative"] += 1
            else: skip[reason[:40]] += 1
        lt = sum(1 for r in wt if r.get("shadow_side") == "long")
        st = sum(1 for r in wt if r.get("shadow_side") == "short")
        results[cn] = {
            "total": len(recs), "would_trade": len(wt),
            "trade_rate": f"{len(wt)/len(recs)*100:.1f}%" if recs else "0%",
            "long_trades": lt, "short_trades": st,
            "skip_reasons": dict(skip.most_common()),
        }
    return results


# ---------------------------------------------------------------------------
# Gate analysis
# ---------------------------------------------------------------------------
def analyze_gates(shadow: list) -> dict:
    skip = Counter()
    for r in shadow:
        if not r.get("would_trade") and r.get("shadow_skip_reason"):
            reason = r["shadow_skip_reason"]
            if "kelly" in reason.lower(): skip["kelly_no_edge"] += 1
            elif "markov" in reason.lower(): skip["markov_below_threshold"] += 1
            elif "direction" in reason.lower(): skip["direction_blocked"] += 1
            elif "liquidity" in reason.lower(): skip["liquidity_guard"] += 1
            elif "fee" in reason.lower(): skip["fee_aware_ev_negative"] += 1
            else: skip[reason[:40]] += 1
    total = sum(skip.values())
    return {"total_skips": total, "by_gate": {
        r: {"count": c, "pct": f"{c/total*100:.1f}%"} for r, c in skip.most_common()
    }}


# ---------------------------------------------------------------------------
# Fee analysis
# ---------------------------------------------------------------------------
def analyze_fee(shadow: list) -> dict:
    fee_recs = [r for r in shadow if r.get("fee_estimate") is not None]
    if not fee_recs:
        # Check if fee_schedule_rate is available
        schedule_recs = [r for r in shadow if r.get("fee_schedule_rate") is not None]
        return {
            "status": "partial",
            "fee_schedule_rate_available": len(schedule_recs) > 0,
            "fee_estimate_available": False,
            "recommendation": "fee_schedule_rate from Gamma API available as upper bound",
        }
    return {"status": "available", "count": len(fee_recs)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_analysis(date_str: str = "", use_all: bool = False) -> dict:
    journal = load_journal_records(date_str if not use_all else "")
    shadow = load_shadow_records(date_str if not use_all else "")
    return {
        "journal_records": len(journal),
        "shadow_records": len(shadow),
        "p_model_calibration": analyze_p_model_calibration(journal),
        "direction": analyze_direction(journal),
        "entry_prices": analyze_entry_prices(journal),
        "shadow": analyze_shadow(shadow),
        "gates": analyze_gates(shadow),
        "regime": analyze_regime(shadow),
        "regime_distribution": analyze_regime_from_shadow_all(shadow),
        "fee": analyze_fee(shadow),
    }


def print_analysis(r: dict, date_str: str = ""):
    print("=" * 70)
    print("COMPREHENSIVE DIAGNOSTIC ANALYSIS")
    print(f"Date: {date_str}" if date_str else "Date: ALL")
    print("=" * 70)
    print(f"\nData: {r['journal_records']} journal, {r['shadow_records']} shadow")

    # p_model
    print("\n--- p_model CALIBRATION ---")
    pm = r["p_model_calibration"]
    if "error" in pm: print(f"  {pm['error']}")
    else:
        print(f"  {'Bucket':<12} {'Trades':>7} {'WinRate':>8} {'P/L':>8} {'Long':>5} {'Short':>5} {'Gap':>8}")
        print("  " + "-" * 65)
        for b, d in sorted(pm.items()):
            print(f"  {b:<12} {d['trades']:>7} {d['win_rate']:>8} {d['total_pnl']:>8.4f} {d['long']:>5} {d['short']:>5} {d['calibration_gap']:>8.4f}")

    # Direction
    print("\n--- DIRECTION PERFORMANCE ---")
    dp = r["direction"]
    if "error" in dp: print(f"  {dp['error']}")
    else:
        for d, v in sorted(dp.items()):
            print(f"  {d:<8} {v['trades']:>5} trades, {v['win_rate']:>5} WR, P/L: ${v['total_pnl']:>+.4f}")

    # Entry prices
    print("\n--- ENTRY PRICE BUCKETS ---")
    ep = r["entry_prices"]
    if "error" in ep: print(f"  {ep['error']}")
    else:
        for b, d in sorted(ep.items()):
            print(f"  {b:<12} {d['trades']:>5} trades, {d['win_rate']:>5} WR, P/L: ${d['total_pnl']:>+.4f}")

    # Regime distribution
    print("\n--- BTC REGIME DISTRIBUTION ---")
    rd = r["regime_distribution"]
    print(f"  Regimes: {rd.get('regime_distribution', {})}")
    pr = rd.get("btc_price_range", {})
    if pr.get("count"):
        print(f"  BTC price range: ${pr['min']:.2f} - ${pr['max']:.2f} ({pr['count']} records)")

    # Regime x Direction
    print("\n--- REGIME x DIRECTION (shadow would_trade) ---")
    rx = r["regime"]
    if rx:
        for regime, sides in sorted(rx.items()):
            for side, data in sorted(sides.items()):
                print(f"  {regime:<12} {side:<8} {data['shadow_would_trade']:>3} would-trade")
    else:
        print("  No authorized trade shadow records with regime data yet")

    # Shadow
    print("\n--- SHADOW ANALYSIS ---")
    sh = r["shadow"]
    if "error" in sh: print(f"  {sh['error']}")
    else:
        print(f"  {'Config':<25} {'Total':>6} {'Trade':>6} {'Rate':>7} {'Long':>5} {'Short':>5}")
        print("  " + "-" * 60)
        for cn in ["baseline", "min_prob_082", "min_prob_084", "markov_window_10",
                     "markov_window_15", "short_only", "long_only"]:
            if cn in sh:
                d = sh[cn]
                print(f"  {cn:<25} {d['total']:>6} {d['would_trade']:>6} {d['trade_rate']:>7} {d['long_trades']:>5} {d['short_trades']:>5}")

    # Gates
    print("\n--- GATE ANALYSIS ---")
    g = r["gates"]
    print(f"  Total skipped: {g['total_skips']}")
    for reason, d in g["by_gate"].items():
        print(f"  {reason:<30} {d['count']:>6} ({d['pct']})")

    # Fee
    print("\n--- FEE STATUS ---")
    f = r["fee"]
    print(f"  Status: {f.get('status', 'unknown')}")
    if f.get("recommendation"): print(f"  Recommendation: {f['recommendation']}")

    print()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--all":
        print_analysis(run_analysis(use_all=True))
    elif len(sys.argv) > 1:
        print_analysis(run_analysis(sys.argv[1]), sys.argv[1])
    else:
        print_analysis(run_analysis(use_all=True))
