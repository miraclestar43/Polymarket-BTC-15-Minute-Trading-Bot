#!/usr/bin/env python3
"""
Shadow Outcome Analysis

Resolves shadow would_trade candidates directly using Gamma API.
Computes hypothetical P/L for each shadow config.

Resolution is INDEPENDENT of actual simulated orders.
Each shadow candidate is resolved by its market_id.

Usage:
    python scripts/analyze_shadow_outcomes.py [YYYY-MM-DD]
    python scripts/analyze_shadow_outcomes.py --all
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import List, Dict, Any, Optional

# Add parent dir to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.strategy_brain.market_resolver import get_market_resolver


def load_shadow_records(date_str: str = "", log_dir: str = "logs") -> list:
    """Load shadow decision records."""
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


def resolve_shadow_candidate(
    candidate: Dict[str, Any],
    resolver: Any,
) -> Dict[str, Any]:
    """
    Resolve a shadow would_trade candidate directly.
    Returns resolution status and hypothetical outcome.
    """
    if not candidate.get("would_trade"):
        return {"status": "not_a_trade"}

    market_id = candidate.get("market_id", "")
    shadow_side = candidate.get("shadow_side", "unknown")
    entry_price = candidate.get("entry_price", 0)

    # Resolve market using Gamma API
    resolution = resolver.resolve(market_id=market_id)

    status = resolution.get("resolution_status", "unavailable")
    resolved_outcome = resolution.get("resolved_outcome")

    if status == "resolved" and resolved_outcome:
        # Compute hypothetical outcome based on shadow side
        # Polymarket: YES = Up, NO = Down
        # LONG/BUY YES wins if outcome is YES/Up
        # SHORT/BUY NO wins if outcome is NO/Down

        if shadow_side == "long":
            # BUY YES wins if outcome is YES
            hypothetical_win = resolved_outcome in ("YES", "UP")
        elif shadow_side == "short":
            # BUY NO wins if outcome is NO
            hypothetical_win = resolved_outcome in ("NO", "DOWN")
        else:
            hypothetical_win = None

        # Compute hypothetical P/L
        # If win: P/L = (1 - entry_price) * size (simplified)
        # If loss: P/L = -entry_price * size
        # Using $5 position size for consistency
        position_size = 5.0
        if hypothetical_win:
            hypothetical_pnl = (1.0 - entry_price) * position_size / entry_price
        else:
            hypothetical_pnl = -position_size

        return {
            "status": "resolved",
            "shadow_side": shadow_side,
            "entry_price": entry_price,
            "resolved_outcome": resolved_outcome,
            "hypothetical_win": hypothetical_win,
            "hypothetical_pnl": round(hypothetical_pnl, 4),
            "p_model": candidate.get("p_model"),
            "raw_kelly_fraction": candidate.get("raw_kelly_fraction"),
            "kelly_gap": candidate.get("kelly_gap"),
            "btc_regime": candidate.get("btc_regime"),
            "config_name": candidate.get("config_name"),
        }
    else:
        return {
            "status": status,
            "shadow_side": shadow_side,
            "entry_price": entry_price,
            "resolution_error": resolution.get("resolution_error"),
            "config_name": candidate.get("config_name"),
        }


def analyze_config_outcomes(
    shadow_records: list,
) -> Dict[str, Any]:
    """Analyze hypothetical outcomes by shadow config."""
    resolver = get_market_resolver()
    candidates = [r for r in shadow_records if r.get("would_trade")]

    results = {}
    for config_name in [
        "baseline", "min_prob_082", "min_prob_084",
        "markov_window_10", "markov_window_15",
        "short_only", "long_only",
        "shadow_no_kelly", "shadow_allow_kelly_minus_002", "shadow_allow_kelly_minus_005",
    ]:
        config_candidates = [r for r in candidates if r.get("config_name") == config_name]
        if not config_candidates:
            continue

        outcomes = []
        for c in config_candidates:
            outcome = resolve_shadow_candidate(c, resolver)
            outcomes.append(outcome)

        resolved = [o for o in outcomes if o.get("status") == "resolved"]
        pending = [o for o in outcomes if o.get("status") == "pending"]
        unavailable = [o for o in outcomes if o.get("status") in ("unavailable", "error")]
        not_a_trade = [o for o in outcomes if o.get("status") == "not_a_trade"]

        if resolved:
            wins = sum(1 for o in resolved if o.get("hypothetical_win"))
            total_pnl = sum(o.get("hypothetical_pnl", 0) for o in resolved)
            win_rate = wins / len(resolved) * 100 if resolved else 0

            long_trades = [o for o in resolved if o.get("shadow_side") == "long"]
            short_trades = [o for o in resolved if o.get("shadow_side") == "short"]

            long_wins = sum(1 for o in long_trades if o.get("hypothetical_win"))
            short_wins = sum(1 for o in short_trades if o.get("hypothetical_win"))
            long_pnl = sum(o.get("hypothetical_pnl", 0) for o in long_trades)
            short_pnl = sum(o.get("hypothetical_pnl", 0) for o in short_trades)
        else:
            wins = 0
            total_pnl = 0
            win_rate = 0
            long_trades = []
            short_trades = []
            long_wins = 0
            short_wins = 0
            long_pnl = 0
            short_pnl = 0

        results[config_name] = {
            "total_candidates": len(config_candidates),
            "resolved": len(resolved),
            "pending": len(pending),
            "unavailable": len(unavailable),
            "wins": wins,
            "losses": len(resolved) - wins,
            "win_rate": f"{win_rate:.1f}%",
            "total_pnl": round(total_pnl, 4),
            "avg_pnl": round(total_pnl / len(resolved), 4) if resolved else 0,
            "long_trades": len(long_trades),
            "long_wins": long_wins,
            "long_pnl": round(long_pnl, 4),
            "short_trades": len(short_trades),
            "short_wins": short_wins,
            "short_pnl": round(short_pnl, 4),
        }

    return results


def analyze_unresolved_reasons(shadow_records: list) -> Dict[str, int]:
    """Analyze why shadow candidates are unresolved."""
    resolver = get_market_resolver()
    candidates = [r for r in shadow_records if r.get("would_trade")]

    reasons = Counter()
    for c in candidates:
        outcome = resolve_shadow_candidate(c, resolver)
        status = outcome.get("status", "unknown")
        reasons[status] += 1

    return dict(reasons.most_common())


def print_analysis(results: Dict[str, Any], unresolved: Dict[str, int], date_str: str = ""):
    """Print shadow outcome analysis."""
    print("=" * 70)
    print("SHADOW OUTCOME ANALYSIS (Independent Resolution)")
    print(f"Date: {date_str}" if date_str else "Date: ALL")
    print("=" * 70)

    config_results = results.get("by_config", {})

    print("\n--- HYPOTHETICAL P/L BY CONFIG ---")
    print(f"  {'Config':<30} {'Total':>6} {'Resol':>6} {'Pend':>5} {'Unavail':>7} {'Wins':>5} {'WR':>7} {'P/L':>10}")
    print("  " + "-" * 90)
    for config in [
        "baseline", "min_prob_082", "min_prob_084",
        "markov_window_10", "markov_window_15",
        "short_only", "long_only",
        "shadow_no_kelly", "shadow_allow_kelly_minus_002", "shadow_allow_kelly_minus_005",
    ]:
        if config in config_results:
            d = config_results[config]
            print(f"  {config:<30} {d['total_candidates']:>6} {d['resolved']:>6} {d['pending']:>5} {d['unavailable']:>7} {d['wins']:>5} {d['win_rate']:>7} ${d['total_pnl']:>9.4f}")

    if unresolved:
        print("\n--- UNRESOLVED REASONS ---")
        for reason, count in unresolved.items():
            print(f"  {reason:<20} {count:>5}")

    print()


if __name__ == "__main__":
    date_str = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "--all" else ""
    use_all = len(sys.argv) > 1 and sys.argv[1] == "--all"

    shadow = load_shadow_records(date_str if not use_all else "")

    config_results = analyze_config_outcomes(shadow)
    unresolved = analyze_unresolved_reasons(shadow)

    results = {
        "by_config": config_results,
    }

    print_analysis(results, unresolved, date_str)
