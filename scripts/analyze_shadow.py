#!/usr/bin/env python3
"""
Shadow Evaluation Analysis

Reads shadow_decisions_YYYY-MM-DD.jsonl and summarizes results.
No Telegram, no strategy changes, no .env access.

Usage:
    python scripts/analyze_shadow.py [YYYY-MM-DD]
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timezone


def load_shadow_records(date_str: str = "", log_dir: str = "logs") -> list:
    """Load shadow decision records for a given date."""
    if not date_str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    log_path = Path(log_dir) / f"shadow_decisions_{date_str}.jsonl"
    if not log_path.exists():
        return []

    records = []
    with open(log_path) as f:
        for line in f:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def analyze_shadows(records: list) -> dict:
    """Analyze shadow decision records."""
    if not records:
        return {"error": "No shadow records found"}

    # Group by config
    by_config = defaultdict(list)
    for r in records:
        by_config[r.get("config_name", "unknown")].append(r)

    results = {}

    for config_name, config_records in by_config.items():
        would_trade = sum(1 for r in config_records if r.get("would_trade"))
        total = len(config_records)
        trade_rate = would_trade / total * 100 if total > 0 else 0

        # Skip reasons
        skip_reasons = Counter()
        for r in config_records:
            if not r.get("would_trade") and r.get("shadow_skip_reason"):
                reason = r["shadow_skip_reason"]
                if "markov" in reason.lower():
                    skip_reasons["markov"] += 1
                elif "kelly" in reason.lower():
                    skip_reasons["kelly"] += 1
                elif "direction" in reason.lower():
                    skip_reasons["direction"] += 1
                elif "liquidity" in reason.lower():
                    skip_reasons["liquidity"] += 1
                elif "fee" in reason.lower():
                    skip_reasons["fee"] += 1
                else:
                    skip_reasons[reason[:40]] += 1

        # Direction breakdown
        long_trades = sum(1 for r in config_records if r.get("would_trade") and r.get("shadow_side") == "long")
        short_trades = sum(1 for r in config_records if r.get("would_trade") and r.get("shadow_side") == "short")

        # Actual event type breakdown
        actual_events = Counter(r.get("actual_event_type", "?") for r in config_records)

        results[config_name] = {
            "total": total,
            "would_trade": would_trade,
            "trade_rate": f"{trade_rate:.1f}%",
            "long_trades": long_trades,
            "short_trades": short_trades,
            "skip_reasons": dict(skip_reasons.most_common()),
            "actual_events": dict(actual_events),
        }

    return results


def print_analysis(results: dict, date_str: str = ""):
    """Print shadow analysis results."""
    print("=" * 60)
    print("SHADOW EVALUATION ANALYSIS")
    if date_str:
        print(f"Date: {date_str}")
    print("=" * 60)

    if "error" in results:
        print(f"  {results['error']}")
        return

    # Summary table
    print(f"\n{'Config':<25} {'Total':>6} {'Trade':>6} {'Rate':>7} {'Long':>5} {'Short':>5}")
    print("-" * 60)
    for config_name in ["baseline", "min_prob_082", "min_prob_084",
                         "markov_window_10", "markov_window_15",
                         "short_only", "long_only"]:
        if config_name in results:
            r = results[config_name]
            print(f"{config_name:<25} {r['total']:>6} {r['would_trade']:>6} {r['trade_rate']:>7} {r['long_trades']:>5} {r['short_trades']:>5}")

    # Comparison with actual
    if "baseline" in results:
        baseline = results["baseline"]
        print(f"\nActual strategy comparison:")
        print(f"  Baseline shadow: {baseline['would_trade']} would-trade out of {baseline['total']} cycles")
        print(f"  Actual events: {baseline['actual_events']}")

    # Skip reasons for baseline
    if "baseline" in results and results["baseline"]["skip_reasons"]:
        print(f"\nBaseline shadow skip reasons:")
        for reason, count in sorted(results["baseline"]["skip_reasons"].items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")

    # Key comparisons
    if "baseline" in results and "min_prob_082" in results:
        b = results["baseline"]["would_trade"]
        m82 = results["min_prob_082"]["would_trade"]
        print(f"\nmin_prob=0.82 would add: {m82 - b} extra trades")

    if "baseline" in results and "min_prob_084" in results:
        b = results["baseline"]["would_trade"]
        m84 = results["min_prob_084"]["would_trade"]
        print(f"min_prob=0.84 would add: {m84 - b} extra trades")

    if "baseline" in results and "short_only" in results:
        b = results["baseline"]["would_trade"]
        s = results["short_only"]["would_trade"]
        print(f"SHORT-only would change: {s} trades (from {b})")

    if "baseline" in results and "long_only" in results:
        b = results["baseline"]["would_trade"]
        l = results["long_only"]["would_trade"]
        print(f"LONG-only would change: {l} trades (from {b})")

    print()


if __name__ == "__main__":
    date_str = sys.argv[1] if len(sys.argv) > 1 else ""
    records = load_shadow_records(date_str)
    results = analyze_shadows(records)
    print_analysis(results, date_str)
