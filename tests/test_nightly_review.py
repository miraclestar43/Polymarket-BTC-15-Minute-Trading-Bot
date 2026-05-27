"""
Safe tests for Nightly Review.

These tests:
  - Do NOT place real orders
  - Do NOT require API keys or wallet access
  - Do NOT modify strategy.yaml (uses temp files)
  - Can be run offline
"""
import sys
import os
import json
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from scripts.nightly_review import (
    analyze_records,
    generate_recommendations,
    format_telegram_summary,
    backup_strategy_yaml,
)


def _make_records(events):
    """Helper to create journal records from event specs."""
    records = []
    for event in events:
        r = {"event_type": event.get("event_type", "decision"), "dry_run": True}
        r.update(event)
        records.append(r)
    return records


def test_empty_journal():
    """Empty journal → no crashes, zero everything."""
    records = []
    analysis = analyze_records(records)
    assert analysis["total_records"] == 0
    assert analysis["simulated_orders"] == 0
    assert analysis["total_pnl"] == 0.0
    print("  ✓ Empty journal handled correctly")


def test_only_skipped_trades():
    """Journal with only skipped trades → correct counts."""
    records = _make_records([
        {"event_type": "skipped_trade", "decision_reason": "markov_persistence_below_threshold"},
        {"event_type": "skipped_trade", "decision_reason": "kelly_no_edge"},
        {"event_type": "skipped_trade", "decision_reason": "trend_neutral"},
    ])
    analysis = analyze_records(records)
    assert analysis["skipped_trades"] == 3
    assert analysis["markov_blocked"] == 1
    assert analysis["kelly_blocked"] == 1
    assert analysis["simulated_orders"] == 0
    print("  ✓ Skipped trades counted correctly")


def test_winning_losing_trades():
    """Journal with mixed winning and losing trades."""
    records = _make_records([
        {"event_type": "simulated_order", "simulated_pnl": 1.50, "entry_price": 0.72, "markov_state": "BULLISH"},
        {"event_type": "simulated_order", "simulated_pnl": -0.50, "entry_price": 0.45, "markov_state": "BEARISH"},
        {"event_type": "simulated_order", "simulated_pnl": 2.00, "entry_price": 0.68, "markov_state": "BULLISH"},
        {"event_type": "simulated_order", "simulated_pnl": -1.00, "entry_price": 0.55, "markov_state": "BULLISH"},
    ])
    analysis = analyze_records(records)
    assert analysis["simulated_orders"] == 4
    assert analysis["win_count"] == 2
    assert analysis["loss_count"] == 2
    assert analysis["total_pnl"] == 2.0  # 1.50 - 0.50 + 2.00 - 1.00
    assert analysis["win_rate"] == 0.5
    print("  ✓ Winning/losing trades analyzed correctly")


def test_markov_state_win_rates():
    """Markov state win rates are computed correctly."""
    records = _make_records([
        {"event_type": "simulated_order", "simulated_pnl": 1.0, "markov_state": "BULLISH"},
        {"event_type": "simulated_order", "simulated_pnl": 1.0, "markov_state": "BULLISH"},
        {"event_type": "simulated_order", "simulated_pnl": -0.5, "markov_state": "BULLISH"},
        {"event_type": "simulated_order", "simulated_pnl": -1.0, "markov_state": "BEARISH"},
    ])
    analysis = analyze_records(records)
    assert analysis["markov_states"]["BULLISH"]["wins"] == 2
    assert analysis["markov_states"]["BULLISH"]["losses"] == 1
    assert analysis["markov_states"]["BEARISH"]["wins"] == 0
    assert analysis["markov_states"]["BEARISH"]["losses"] == 1
    assert analysis["best_markov_state"] == "BULLISH"
    print("  ✓ Markov state win rates correct")


def test_entry_price_range_ev():
    """Entry price range EV is computed correctly."""
    records = _make_records([
        {"event_type": "simulated_order", "simulated_pnl": 2.0, "entry_price": 0.72},
        {"event_type": "simulated_order", "simulated_pnl": 1.0, "entry_price": 0.75},
        {"event_type": "simulated_order", "simulated_pnl": -1.0, "entry_price": 0.45},
    ])
    analysis = analyze_records(records)
    # 0.70-0.80 range: 2 trades, pnl=3.0, EV=1.5
    assert "0.70-0.80" in analysis["entry_price_ranges"]
    assert analysis["entry_price_ranges"]["0.70-0.80"]["count"] == 2
    assert analysis["entry_price_ranges"]["0.70-0.80"]["pnl"] == 3.0
    # 0.40-0.50 range: 1 trade, pnl=-1.0
    assert "0.40-0.50" in analysis["entry_price_ranges"]
    print("  ✓ Entry price range EV correct")


def test_min_prob_recommendation_with_data():
    """min_prob recommendation with enough data."""
    current_config = {"min_prob": 0.87, "min_prob_min": 0.82, "min_prob_max": 0.95, "min_prob_daily_step": 0.02}
    analysis = {"simulated_orders": 25, "total_pnl": 3.0, "win_rate": 0.65, "avg_p_stay": 0.92}
    recs = generate_recommendations(analysis, current_config)
    # Good performance → loosen min_prob
    assert recs["min_prob"]["recommended"] < 0.87
    print(f"  ✓ min_prob loosened: {recs['min_prob']['recommended']} ({recs['min_prob']['reason']})")


def test_min_prob_no_lower_when_sample_small():
    """min_prob does not lower when sample size is too small."""
    current_config = {"min_prob": 0.87, "min_prob_min": 0.82, "min_prob_max": 0.95, "min_prob_daily_step": 0.02}
    analysis = {"simulated_orders": 10, "total_pnl": 5.0, "win_rate": 0.80, "avg_p_stay": 0.95}
    recs = generate_recommendations(analysis, current_config)
    # Small sample → keep current
    assert recs["min_prob"]["recommended"] == 0.87
    print(f"  ✓ min_prob kept (small sample): {recs['min_prob']['recommended']}")


def test_kelly_cap_within_bounds():
    """Kelly cap recommendation stays within bounds."""
    current_config = {
        "kelly_fraction_cap": 0.05,
        "kelly_fraction_cap_min": 0.01,
        "kelly_fraction_cap_max": 0.10,
        "kelly_fraction_cap_daily_step": 0.01,
    }
    analysis = {"simulated_orders": 25, "total_pnl": 10.0, "win_rate": 0.70, "avg_p_stay": 0.90}
    recs = generate_recommendations(analysis, current_config)
    assert 0.01 <= recs["kelly_fraction_cap"]["recommended"] <= 0.10
    print(f"  ✓ Kelly cap in bounds: {recs['kelly_fraction_cap']['recommended']}")


def test_strategy_update_creates_backup():
    """Strategy update creates a backup file."""
    tmpdir = tempfile.mkdtemp()
    try:
        # Create a fake strategy.yaml
        fake_yaml = os.path.join(tmpdir, "strategy.yaml")
        with open(fake_yaml, "w") as f:
            f.write("min_prob: 0.87\nkelly_fraction_cap: 0.05\n")

        # Backup
        backup = backup_strategy_yaml(config_dir=tmpdir)
        assert os.path.exists(backup)
        assert "strategy_" in backup
        assert backup.endswith(".yaml")

        # Verify backup content
        with open(backup, "r") as f:
            content = f.read()
        assert "min_prob: 0.87" in content
        print(f"  ✓ Backup created: {Path(backup).name}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_telegram_summary_formatting():
    """Telegram summary formats without exposing secrets."""
    analysis = {
        "total_pnl": 2.50,
        "win_rate": 0.60,
        "win_count": 3,
        "loss_count": 2,
        "simulated_orders": 5,
        "skipped_trades": 3,
        "markov_blocked": 1,
        "kelly_blocked": 1,
        "best_markov_state": "BULLISH",
        "best_markov_win_rate": 0.67,
        "best_entry_range": "0.70-0.80",
        "best_entry_ev": 1.25,
    }
    recommendations = {
        "min_prob": {"current": 0.87, "recommended": 0.85, "reason": "loosening"},
        "kelly_fraction_cap": {"current": 0.05, "recommended": 0.05, "reason": "no change"},
        "min_edge": {"current": 0.05, "recommended": 0.05, "reason": "no change"},
    }
    text = format_telegram_summary("2026-05-27", analysis, recommendations, applied=False)
    assert "2026-05-27" in text
    assert "2.50" in text
    assert "60.0%" in text
    assert "min_prob" in text
    assert "Recommendations only" in text or "strategy.yaml" in text
    # No secrets
    assert "TELEGRAM" not in text
    assert "private" not in text.lower()
    print("  ✓ Telegram summary formatted correctly, no secrets")


def test_no_env_values_printed():
    """Verify analysis doesn't reference .env values."""
    records = _make_records([
        {"event_type": "simulated_order", "simulated_pnl": 1.0, "markov_state": "BULLISH"},
    ])
    analysis = analyze_records(records)
    # Just verify the analysis dict doesn't contain env-like keys
    for key in analysis:
        assert "token" not in key.lower(), f"Analysis contains key '{key}'"
        assert "secret" not in key.lower(), f"Analysis contains key '{key}'"
    print("  ✓ No .env values in analysis")


if __name__ == "__main__":
    print("=" * 60)
    print("Nightly Review — Safe Tests")
    print("=" * 60)

    tests = [
        test_empty_journal,
        test_only_skipped_trades,
        test_winning_losing_trades,
        test_markov_state_win_rates,
        test_entry_price_range_ev,
        test_min_prob_recommendation_with_data,
        test_min_prob_no_lower_when_sample_small,
        test_kelly_cap_within_bounds,
        test_strategy_update_creates_backup,
        test_telegram_summary_formatting,
        test_no_env_values_printed,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  ✗ FAILED: {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ ERROR: {test.__name__}: {e}")
            failed += 1

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed, {len(tests)} total")
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)
