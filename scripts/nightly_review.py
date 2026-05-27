#!/usr/bin/env python3
"""
Nightly Review and Strategy Recommendation

Reads today's trade journal, analyzes performance, sends Telegram
summary, and generates recommended strategy parameter updates.

Safety:
  - DRY_RUN=true by default
  - Never modifies .env
  - strategy.yaml only updated if auto_update_enabled=true AND --apply
  - Always backs up strategy.yaml before any update
  - Never prints secrets

Usage:
    python scripts/nightly_review.py
    python scripts/nightly_review.py --date 2026-05-27
    python scripts/nightly_review.py --dry-run --no-telegram
    python scripts/nightly_review.py --apply
"""
import sys
import os
import json
import shutil
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import yaml
from dotenv import load_dotenv

load_dotenv()

from journal.trade_journal import daily_summary
from notifications.telegram_notifier import send_daily_summary, is_configured


# ---------------------------------------------------------------------------
# Journal reading
# ---------------------------------------------------------------------------
def read_journal_records(date_str: str) -> List[Dict[str, Any]]:
    """Read all journal records for a given date."""
    config_path = project_root / "strategy.yaml"
    with open(config_path, "r") as f:
        config = yaml.safe_load(f) or {}
    base_path = config.get("journal_path", "logs/trade_journal")
    file_path = f"{base_path}_{date_str}.jsonl"

    records = []
    if not os.path.exists(file_path):
        return records

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def analyze_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze journal records and compute performance metrics."""
    analysis = {
        "total_records": len(records),
        "decisions": 0,
        "simulated_orders": 0,
        "skipped_trades": 0,
        "rejected_orders": 0,
        "markov_blocked": 0,
        "kelly_blocked": 0,
        "pnl_values": [],
        "p_stay_values": [],
        "p_model_values": [],
        "kelly_sizes": [],
        "entry_prices": [],
        "markov_states": {},
        "entry_price_ranges": {},
    }

    for r in records:
        event = r.get("event_type", "")

        if event == "decision":
            analysis["decisions"] += 1
        elif event == "simulated_order":
            analysis["simulated_orders"] += 1
            pnl = r.get("simulated_pnl")
            if pnl is not None:
                analysis["pnl_values"].append(pnl)
            ep = r.get("entry_price")
            if ep is not None:
                analysis["entry_prices"].append(ep)
        elif event == "skipped_trade":
            analysis["skipped_trades"] += 1
            reason = r.get("decision_reason", "")
            if "markov" in reason.lower():
                analysis["markov_blocked"] += 1
            elif "kelly" in reason.lower() or "negative_kelly" in reason.lower():
                analysis["kelly_blocked"] += 1
        elif event == "order_rejected":
            analysis["rejected_orders"] += 1

        # Collect numeric fields
        ps = r.get("p_stay")
        if ps is not None:
            analysis["p_stay_values"].append(ps)
        pm = r.get("p_model")
        if pm is not None:
            analysis["p_model_values"].append(pm)
        ks = r.get("kelly_size")
        if ks is not None:
            analysis["kelly_sizes"].append(ks)

    # Markov state win rates
    for r in records:
        if r.get("event_type") == "simulated_order":
            state = r.get("markov_state", "UNKNOWN")
            if state not in analysis["markov_states"]:
                analysis["markov_states"][state] = {"wins": 0, "losses": 0, "total": 0}
            analysis["markov_states"][state]["total"] += 1
            pnl = r.get("simulated_pnl", 0)
            if pnl > 0:
                analysis["markov_states"][state]["wins"] += 1
            elif pnl < 0:
                analysis["markov_states"][state]["losses"] += 1

    # Entry price range EV
    for r in records:
        if r.get("event_type") == "simulated_order":
            ep = r.get("entry_price")
            pnl = r.get("simulated_pnl", 0)
            if ep is not None:
                # Bucket into ranges: 0.40-0.50, 0.50-0.60, 0.60-0.70, 0.70-0.80, 0.80-0.90
                if ep < 0.50:
                    bucket = "0.40-0.50"
                elif ep < 0.60:
                    bucket = "0.50-0.60"
                elif ep < 0.70:
                    bucket = "0.60-0.70"
                elif ep < 0.80:
                    bucket = "0.70-0.80"
                else:
                    bucket = "0.80-0.90"
                if bucket not in analysis["entry_price_ranges"]:
                    analysis["entry_price_ranges"][bucket] = {"pnl": 0.0, "count": 0}
                analysis["entry_price_ranges"][bucket]["pnl"] += pnl
                analysis["entry_price_ranges"][bucket]["count"] += 1

    # Summary stats
    analysis["total_pnl"] = sum(analysis["pnl_values"])
    analysis["win_count"] = sum(1 for p in analysis["pnl_values"] if p > 0)
    analysis["loss_count"] = sum(1 for p in analysis["pnl_values"] if p < 0)
    total_trades = analysis["win_count"] + analysis["loss_count"]
    analysis["win_rate"] = analysis["win_count"] / total_trades if total_trades > 0 else 0.0
    analysis["avg_p_stay"] = (
        sum(analysis["p_stay_values"]) / len(analysis["p_stay_values"])
        if analysis["p_stay_values"] else None
    )
    analysis["avg_p_model"] = (
        sum(analysis["p_model_values"]) / len(analysis["p_model_values"])
        if analysis["p_model_values"] else None
    )
    analysis["avg_kelly_size"] = (
        sum(analysis["kelly_sizes"]) / len(analysis["kelly_sizes"])
        if analysis["kelly_sizes"] else None
    )

    # Best/worst Markov states
    best_state = None
    best_wr = -1
    worst_state = None
    worst_wr = 2.0
    for state, data in analysis["markov_states"].items():
        if data["total"] > 0:
            wr = data["wins"] / data["total"]
            if wr > best_wr:
                best_wr = wr
                best_state = state
            if wr < worst_wr:
                worst_wr = wr
                worst_state = state
    analysis["best_markov_state"] = best_state
    analysis["best_markov_win_rate"] = best_wr if best_state else None
    analysis["worst_markov_state"] = worst_state
    analysis["worst_markov_win_rate"] = worst_wr if worst_state else None

    # Best entry price range
    best_range = None
    best_ev = -999
    for bucket, data in analysis["entry_price_ranges"].items():
        if data["count"] > 0:
            ev = data["pnl"] / data["count"]
            if ev > best_ev:
                best_ev = ev
                best_range = bucket
    analysis["best_entry_range"] = best_range
    analysis["best_entry_ev"] = best_ev if best_range else None

    return analysis


# ---------------------------------------------------------------------------
# Strategy recommendations
# ---------------------------------------------------------------------------
def generate_recommendations(
    analysis: Dict[str, Any],
    current_config: Dict[str, Any],
) -> Dict[str, Any]:
    """Generate strategy parameter recommendations based on analysis."""
    recs = {
        "min_prob": {"current": current_config.get("min_prob", 0.87), "recommended": None, "reason": ""},
        "kelly_fraction_cap": {"current": current_config.get("kelly_fraction_cap", 0.05), "recommended": None, "reason": ""},
        "min_edge": {"current": current_config.get("min_edge", 0.05), "recommended": None, "reason": ""},
    }

    # Safety boundaries
    min_prob_min = current_config.get("min_prob_min", 0.82)
    min_prob_max = current_config.get("min_prob_max", 0.95)
    min_prob_step = current_config.get("min_prob_daily_step", 0.02)
    kelly_min = current_config.get("kelly_fraction_cap_min", 0.01)
    kelly_max = current_config.get("kelly_fraction_cap_max", 0.10)
    kelly_step = current_config.get("kelly_fraction_cap_daily_step", 0.01)

    sample_size = analysis.get("simulated_orders", 0)
    total_pnl = analysis.get("total_pnl", 0)
    win_rate = analysis.get("win_rate", 0)
    avg_p_stay = analysis.get("avg_p_stay")

    # --- min_prob recommendation ---
    current_min_prob = recs["min_prob"]["current"]

    if sample_size < 20:
        # Too few samples — do not lower min_prob
        recs["min_prob"]["recommended"] = current_min_prob
        recs["min_prob"]["reason"] = f"sample_size={sample_size}<20, keeping current"
    elif total_pnl < 0:
        # Negative P/L — tighten (raise min_prob)
        new_val = min(current_min_prob + min_prob_step, min_prob_max)
        recs["min_prob"]["recommended"] = new_val
        recs["min_prob"]["reason"] = f"negative_pnl={total_pnl:.2f}, tightening"
    elif win_rate >= 0.6 and avg_p_stay and avg_p_stay >= 0.90:
        # Good performance — could loosen slightly
        new_val = max(current_min_prob - min_prob_step, min_prob_min)
        recs["min_prob"]["recommended"] = new_val
        recs["min_prob"]["reason"] = f"win_rate={win_rate:.1%}, avg_p_stay={avg_p_stay:.4f}, loosening"
    else:
        recs["min_prob"]["recommended"] = current_min_prob
        recs["min_prob"]["reason"] = "performance acceptable, no change"

    # Clamp
    recs["min_prob"]["recommended"] = max(min_prob_min, min(min_prob_max, recs["min_prob"]["recommended"]))

    # --- kelly_fraction_cap recommendation ---
    current_kelly = recs["kelly_fraction_cap"]["current"]

    if total_pnl < -5.0:
        # Significant loss — reduce Kelly
        new_val = max(current_kelly - kelly_step, kelly_min)
        recs["kelly_fraction_cap"]["recommended"] = new_val
        recs["kelly_fraction_cap"]["reason"] = f"significant_loss={total_pnl:.2f}, reducing"
    elif win_rate >= 0.65 and sample_size >= 20:
        # Strong performance — could increase slightly
        new_val = min(current_kelly + kelly_step, kelly_max)
        recs["kelly_fraction_cap"]["recommended"] = new_val
        recs["kelly_fraction_cap"]["reason"] = f"win_rate={win_rate:.1%}, increasing"
    else:
        recs["kelly_fraction_cap"]["recommended"] = current_kelly
        recs["kelly_fraction_cap"]["reason"] = "performance acceptable, no change"

    # Clamp
    recs["kelly_fraction_cap"]["recommended"] = max(kelly_min, min(kelly_max, recs["kelly_fraction_cap"]["recommended"]))

    # --- min_edge recommendation ---
    recs["min_edge"]["recommended"] = recs["min_edge"]["current"]
    recs["min_edge"]["reason"] = "not enough data for edge optimization"

    # Notes
    notes = []
    if analysis.get("best_entry_range"):
        notes.append(f"Best entry range: {analysis['best_entry_range']} (EV=${analysis['best_entry_ev']:.4f})")
    if analysis.get("best_markov_state"):
        notes.append(f"Best Markov state: {analysis['best_markov_state']} (win_rate={analysis['best_markov_win_rate']:.1%})")
    if analysis.get("worst_markov_state") and analysis["worst_markov_state"] != analysis.get("best_markov_state"):
        notes.append(f"Worst Markov state: {analysis['worst_markov_state']} (win_rate={analysis['worst_markov_win_rate']:.1%})")

    recs["notes"] = notes
    return recs


# ---------------------------------------------------------------------------
# Strategy update
# ---------------------------------------------------------------------------
def backup_strategy_yaml(config_dir: str = None) -> str:
    """Create a timestamped backup of strategy.yaml."""
    if config_dir is None:
        config_dir = str(project_root / "config" / "strategy_history")
    os.makedirs(config_dir, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    backup_path = os.path.join(config_dir, f"strategy_{timestamp}.yaml")
    shutil.copy2(str(project_root / "strategy.yaml"), backup_path)
    return backup_path


def apply_recommendations(recommendations: Dict[str, Any]) -> bool:
    """Apply recommended changes to strategy.yaml."""
    try:
        # Backup first
        backup_path = backup_strategy_yaml()

        # Read current config
        config_path = project_root / "strategy.yaml"
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}

        # Apply recommendations
        for param in ["min_prob", "kelly_fraction_cap", "min_edge"]:
            rec = recommendations.get(param, {})
            if rec.get("recommended") is not None:
                config[param] = rec["recommended"]

        # Write back
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        return True, backup_path
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Telegram summary
# ---------------------------------------------------------------------------
def format_telegram_summary(
    date_str: str,
    analysis: Dict[str, Any],
    recommendations: Dict[str, Any],
    applied: bool,
) -> str:
    """Format a Telegram summary message."""
    lines = [
        f"🌙 *Nightly Review — {date_str}*",
        "",
        f"Simulated P&L: ${analysis['total_pnl']:+.2f}",
        f"Win rate: {analysis['win_rate']:.1%} ({analysis['win_count']}/{analysis['win_count']+analysis['loss_count']})",
        f"Simulated trades: {analysis['simulated_orders']}",
        f"Skipped trades: {analysis['skipped_trades']}",
        f"Markov blocked: {analysis['markov_blocked']}",
        f"Kelly blocked: {analysis['kelly_blocked']}",
    ]

    if analysis.get("best_markov_state"):
        lines.append(f"Best state: {analysis['best_markov_state']} ({analysis['best_markov_win_rate']:.1%})")
    if analysis.get("best_entry_range"):
        lines.append(f"Best range: {analysis['best_entry_range']} (EV=${analysis['best_entry_ev']:.4f})")

    lines.append("")
    lines.append("*Threshold Changes:*")

    for param in ["min_prob", "kelly_fraction_cap", "min_edge"]:
        rec = recommendations.get(param, {})
        if rec.get("recommended") is not None:
            current = rec["current"]
            recommended = rec["recommended"]
            if current != recommended:
                lines.append(f"  {param}: {current} → {recommended}")
            else:
                lines.append(f"  {param}: {current} (unchanged)")

    lines.append("")
    if applied:
        lines.append("strategy.yaml *updated*")
    else:
        lines.append("Recommendations only (no update)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(description="Nightly review and strategy recommendation")
    parser.add_argument("--date", help="Date in YYYY-MM-DD format (default: today UTC)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Analyze and print without modifying strategy.yaml")
    parser.add_argument("--no-telegram", action="store_true",
                        help="Run analysis without sending Telegram message")
    parser.add_argument("--apply", action="store_true",
                        help="Apply recommended changes (requires auto_update_enabled=true)")
    args = parser.parse_args()

    # Determine date
    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print("=" * 60)
    print(f"Nightly Review — {date_str}")
    print("=" * 60)

    # Read journal
    records = read_journal_records(date_str)
    if not records:
        print(f"No journal records found for {date_str}")
        return

    print(f"Loaded {len(records)} records")

    # Analyze
    analysis = analyze_records(records)

    # Print analysis
    print(f"\nPerformance:")
    print(f"  Simulated P&L: ${analysis['total_pnl']:+.2f}")
    print(f"  Win rate: {analysis['win_rate']:.1%} ({analysis['win_count']}/{analysis['win_count']+analysis['loss_count']})")
    print(f"  Simulated trades: {analysis['simulated_orders']}")
    print(f"  Skipped trades: {analysis['skipped_trades']}")
    print(f"  Markov blocked: {analysis['markov_blocked']}")
    print(f"  Kelly blocked: {analysis['kelly_blocked']}")
    if analysis.get("avg_p_stay") is not None:
        print(f"  Avg p_stay: {analysis['avg_p_stay']:.4f}")
    if analysis.get("avg_p_model") is not None:
        print(f"  Avg p_model: {analysis['avg_p_model']:.4f}")
    if analysis.get("avg_kelly_size") is not None:
        print(f"  Avg Kelly size: ${analysis['avg_kelly_size']:.2f}")

    # Load current config
    config_path = project_root / "strategy.yaml"
    with open(config_path, "r") as f:
        current_config = yaml.safe_load(f) or {}

    # Generate recommendations
    recommendations = generate_recommendations(analysis, current_config)

    print(f"\nRecommendations:")
    for param in ["min_prob", "kelly_fraction_cap", "min_edge"]:
        rec = recommendations[param]
        current = rec["current"]
        recommended = rec["recommended"]
        if current != recommended:
            print(f"  {param}: {current} → {recommended} ({rec['reason']})")
        else:
            print(f"  {param}: {current} (unchanged)")

    for note in recommendations.get("notes", []):
        print(f"  Note: {note}")

    # Apply if requested
    applied = False
    backup_path = None
    if args.apply and not args.dry_run:
        auto_update = current_config.get("auto_update_enabled", False)
        if not auto_update:
            print("\nCannot apply: auto_update_enabled=false in strategy.yaml")
        else:
            success, result = apply_recommendations(recommendations)
            if success:
                applied = True
                backup_path = result
                print(f"\n✓ strategy.yaml updated (backup: {backup_path})")
            else:
                print(f"\n✗ Failed to update strategy.yaml: {result}")
    elif args.dry_run:
        print("\n[DRY_RUN] No changes applied")

    # Write nightly review log
    review_log = {
        "date": date_str,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "analysis": {
            "total_pnl": analysis["total_pnl"],
            "win_rate": analysis["win_rate"],
            "simulated_orders": analysis["simulated_orders"],
            "skipped_trades": analysis["skipped_trades"],
            "markov_blocked": analysis["markov_blocked"],
            "kelly_blocked": analysis["kelly_blocked"],
            "avg_p_stay": analysis["avg_p_stay"],
            "avg_p_model": analysis["avg_p_model"],
            "avg_kelly_size": analysis["avg_kelly_size"],
        },
        "recommendations": {
            k: {"current": v["current"], "recommended": v["recommended"], "reason": v["reason"]}
            for k, v in recommendations.items()
            if isinstance(v, dict) and "recommended" in v
        },
        "applied": applied,
        "backup_path": backup_path,
    }

    log_dir = project_root / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"nightly_review_{date_str}.json"
    with open(log_path, "w") as f:
        json.dump(review_log, f, indent=2, default=str)
    print(f"\nReview log: {log_path}")

    # Send Telegram
    if not args.no_telegram:
        telegram_config = current_config.get("telegram_daily_summary", True)
        if telegram_config and is_configured():
            summary_text = format_telegram_summary(date_str, analysis, recommendations, applied)
            success = send_daily_summary(
                {
                    "date": date_str,
                    "total_simulated_pnl": analysis["total_pnl"],
                    "win_count": analysis["win_count"],
                    "loss_count": analysis["loss_count"],
                    "simulated_orders": analysis["simulated_orders"],
                    "skipped_trades": analysis["skipped_trades"],
                    "markov_blocked": analysis["markov_blocked"],
                    "kelly_blocked": analysis["kelly_blocked"],
                    "avg_p_stay": analysis["avg_p_stay"],
                    "avg_p_model": analysis["avg_p_model"],
                    "avg_kelly_size": analysis["avg_kelly_size"],
                },
                dry_run=args.dry_run,
            )
            if success:
                print("✓ Telegram summary sent")
            else:
                print("✗ Telegram summary failed")
        else:
            print("Telegram daily summary disabled or not configured")

    print("\n" + "=" * 60)
    print("Nightly review complete")
    print("=" * 60)


if __name__ == "__main__":
    main()
