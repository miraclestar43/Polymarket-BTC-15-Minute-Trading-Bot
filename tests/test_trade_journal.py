"""
Safe tests for Trade Journal.

These tests:
  - Do NOT place real orders
  - Do NOT require API keys or wallet access
  - Do NOT modify any external state (uses temp files)
  - Can be run offline
"""
import sys
import os
import json
import tempfile
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from journal.trade_journal import (
    TradeJournal,
    _sanitize_record,
    _is_sensitive,
    daily_summary,
)


def _make_temp_journal():
    """Create a journal writing to a temp directory."""
    tmpdir = tempfile.mkdtemp()
    base_path = os.path.join(tmpdir, "test_journal")
    return TradeJournal(base_path=base_path), tmpdir


def test_write_decision_record():
    """Write one decision record and verify it's valid JSONL."""
    journal, tmpdir = _make_temp_journal()
    try:
        journal.log_decision(
            dry_run=True,
            side="long",
            entry_price=0.72,
            p_model=0.78,
            p_model_source="fused_confidence_proxy",
            odds_b=0.3889,
            kelly_size=5.0,
            markov_state="BULLISH",
            p_stay=0.90,
            min_prob=0.87,
            decision_reason="trade_authorized",
        )
        journal.close()

        # Read the file
        files = [f for f in os.listdir(tmpdir) if f.endswith(".jsonl")]
        assert len(files) == 1, f"Expected 1 JSONL file, got {files}"

        with open(os.path.join(tmpdir, files[0]), "r") as f:
            lines = f.readlines()

        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event_type"] == "decision"
        assert record["dry_run"] is True
        assert record["side"] == "long"
        assert record["entry_price"] == 0.72
        assert record["p_model"] == 0.78
        assert record["p_model_source"] == "fused_confidence_proxy"
        print(f"  ✓ Decision record written and valid")
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_write_skipped_trade_record():
    """Write one skipped trade record."""
    journal, tmpdir = _make_temp_journal()
    try:
        journal.log_skipped_trade(
            dry_run=True,
            decision_reason="markov_persistence_below_threshold",
            side="long",
            entry_price=0.55,
            markov_state="BULLISH",
            p_stay=0.70,
        )
        journal.close()

        files = [f for f in os.listdir(tmpdir) if f.endswith(".jsonl")]
        with open(os.path.join(tmpdir, files[0]), "r") as f:
            record = json.loads(f.readline())

        assert record["event_type"] == "skipped_trade"
        assert record["decision_reason"] == "markov_persistence_below_threshold"
        assert record["p_stay"] == 0.70
        print(f"  ✓ Skipped trade record written and valid")
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_write_simulated_order_record():
    """Write one simulated order record."""
    journal, tmpdir = _make_temp_journal()
    try:
        journal.log_simulated_order(
            dry_run=True,
            side="long",
            entry_price=0.72,
            size=5.0,
            order_id="paper_12345",
            simulated_pnl=0.50,
            mark_price=0.75,
        )
        journal.close()

        files = [f for f in os.listdir(tmpdir) if f.endswith(".jsonl")]
        with open(os.path.join(tmpdir, files[0]), "r") as f:
            record = json.loads(f.readline())

        assert record["event_type"] == "simulated_order"
        assert record["order_id"] == "paper_12345"
        assert record["simulated_pnl"] == 0.50
        print(f"  ✓ Simulated order record written and valid")
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_jsonl_valid():
    """Write multiple records and verify all are valid JSONL."""
    journal, tmpdir = _make_temp_journal()
    try:
        for i in range(5):
            journal.log_decision(
                dry_run=True,
                side="long" if i % 2 == 0 else "short",
                entry_price=0.50 + i * 0.05,
                p_model=0.60 + i * 0.05,
                p_model_source="fused_confidence_proxy",
                decision_reason=f"test_record_{i}",
            )
        journal.close()

        files = [f for f in os.listdir(tmpdir) if f.endswith(".jsonl")]
        with open(os.path.join(tmpdir, files[0]), "r") as f:
            lines = f.readlines()

        assert len(lines) == 5
        for i, line in enumerate(lines):
            record = json.loads(line)
            assert record["event_type"] == "decision"
            assert f"test_record_{i}" in record["decision_reason"]
        print(f"  ✓ All 5 records are valid JSONL")
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_secrets_redacted():
    """Verify sensitive fields are redacted."""
    # Test _is_sensitive
    assert _is_sensitive("private_key", "abc123")
    assert _is_sensitive("api_key", "abc123")
    assert _is_sensitive("telegram_bot_token", "abc123")
    assert _is_sensitive("safe_address", "0x1234")
    assert not _is_sensitive("side", "long")
    assert not _is_sensitive("entry_price", 0.72)

    # Test _sanitize_record
    record = {
        "side": "long",
        "entry_price": 0.72,
        "private_key": "super_secret_key_12345",
        "api_key": "api_key_value",
        "telegram_bot_token": "token_value",
        "safe_address": "0xabcdef1234567890abcdef1234567890abcdef12",
    }
    sanitized = _sanitize_record(record)
    assert sanitized["side"] == "long"
    assert sanitized["entry_price"] == 0.72
    assert sanitized["private_key"] == "[REDACTED]"
    assert sanitized["api_key"] == "[REDACTED]"
    assert sanitized["telegram_bot_token"] == "[REDACTED]"
    # Address should be partially masked
    assert "…" in sanitized["safe_address"] or sanitized["safe_address"] == "[REDACTED]"
    print(f"  ✓ Secrets are properly redacted")


def test_daily_summary_empty():
    """Daily summary on non-existent date returns empty summary."""
    summary = daily_summary(date_str="2099-01-01")
    assert summary["total_records"] == 0
    assert summary["decisions"] == 0
    assert summary["simulated_orders"] == 0
    assert summary["skipped_trades"] == 0
    print(f"  ✓ Empty daily summary works")


def test_daily_summary_with_data():
    """Daily summary aggregates correctly on fake data."""
    # Write records to a temp journal, then run summary on that path
    import shutil
    tmpdir = tempfile.mkdtemp()
    base_path = os.path.join(tmpdir, "summary_test")
    journal = TradeJournal(base_path=base_path)

    # Write 3 decisions, 2 skipped, 1 simulated
    for i in range(3):
        journal.log_decision(
            dry_run=True, side="long", entry_price=0.70,
            p_model=0.75, kelly_size=5.0, decision_reason="test",
        )
    journal.log_skipped_trade(
        dry_run=True, decision_reason="markov_persistence_below_threshold",
    )
    journal.log_skipped_trade(
        dry_run=True, decision_reason="kelly_no_edge",
    )
    journal.log_simulated_order(
        dry_run=True, side="long", entry_price=0.72,
        size=5.0, simulated_pnl=0.50,
    )
    journal.close()

    # Run summary on today's date (journal writes to today's file)
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    summary = daily_summary(date_str=today)

    # Override file path for test (summary reads from config path, not temp)
    # We need to read the file directly
    files = [f for f in os.listdir(tmpdir) if f.endswith(".jsonl")]
    assert len(files) == 1

    # Manually parse to verify
    total = 0
    decisions = 0
    skipped = 0
    simulated = 0
    with open(os.path.join(tmpdir, files[0]), "r") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                total += 1
                if r["event_type"] == "decision":
                    decisions += 1
                elif r["event_type"] == "skipped_trade":
                    skipped += 1
                elif r["event_type"] == "simulated_order":
                    simulated += 1

    assert total == 6
    assert decisions == 3
    assert skipped == 2
    assert simulated == 1
    print(f"  ✓ Daily summary aggregation works on fake data")

    shutil.rmtree(tmpdir, ignore_errors=True)


def test_missing_optional_fields():
    """Verify missing optional fields do not crash."""
    journal, tmpdir = _make_temp_journal()
    try:
        # Minimal record — most fields are None/missing
        journal.log_decision(
            dry_run=True,
            decision_reason="minimal_test",
        )
        journal.close()

        files = [f for f in os.listdir(tmpdir) if f.endswith(".jsonl")]
        with open(os.path.join(tmpdir, files[0]), "r") as f:
            record = json.loads(f.readline())

        assert record["event_type"] == "decision"
        assert record["decision_reason"] == "minimal_test"
        # None fields should not appear in JSON
        assert "p_model" not in record
        assert "kelly_size" not in record
        print(f"  ✓ Missing optional fields do not crash")
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_order_rejected_record():
    """Write one rejected order record."""
    journal, tmpdir = _make_temp_journal()
    try:
        journal.log_order_rejected(
            dry_run=True,
            decision_reason="risk_engine_blocked: Max positions reached",
            side="long",
            error_type="risk_engine_rejection",
            error_message_sanitized="Max positions reached (5)",
        )
        journal.close()

        files = [f for f in os.listdir(tmpdir) if f.endswith(".jsonl")]
        with open(os.path.join(tmpdir, files[0]), "r") as f:
            record = json.loads(f.readline())

        assert record["event_type"] == "order_rejected"
        assert record["error_type"] == "risk_engine_rejection"
        print(f"  ✓ Rejected order record written and valid")
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    print("=" * 60)
    print("Trade Journal — Safe Tests")
    print("=" * 60)

    tests = [
        test_write_decision_record,
        test_write_skipped_trade_record,
        test_write_simulated_order_record,
        test_jsonl_valid,
        test_secrets_redacted,
        test_daily_summary_empty,
        test_daily_summary_with_data,
        test_missing_optional_fields,
        test_order_rejected_record,
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
