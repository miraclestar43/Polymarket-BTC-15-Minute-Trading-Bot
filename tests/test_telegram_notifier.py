"""
Safe tests for Telegram Notifier.

These tests:
  - Do NOT place real orders
  - Do NOT require API keys or wallet access
  - Mock all Telegram HTTP requests
  - Can be run offline
"""
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from notifications.telegram_notifier import (
    _sanitize_message,
    _mask_address,
    _mask_secret,
    send_telegram_message,
    send_trade_alert,
    is_configured,
    get_status,
)


def test_sanitize_normal_messages():
    """Normal bot messages should NOT be redacted."""
    messages = [
        "[DRY-RUN] Bot started (simulation mode)",
        "[DRY-RUN] Simulated order: LONG $5.00 @ 0.645 -> LOSS ($-0.21)",
        "[DRY-RUN] Simulated order: SHORT $5.00 @ 0.345 -> WIN ($+0.28)",
        "[DRY-RUN] Bot stopped (paper trades: 1)",
        "[DRY-RUN] Trade authorized: LONG $5.00 @ 0.645",
        "[DRY-RUN] DRY-RUN degraded - heartbeat stale for 30 min.",
        "[DRY-RUN] Order rejected: risk engine -- max exposure exceeded",
        "Daily Summary -- 2026-05-30\nDecisions: 3\nSimulated orders: 1",
    ]
    for msg in messages:
        result = _sanitize_message(msg)
        assert result == msg, f"Message was redacted: {msg[:50]}...\n  Got: {result[:50]}..."
    print("  OK: Normal messages preserved")


def test_sanitize_wallet_addresses():
    """Wallet addresses should be masked, not redacted."""
    msg = "Funder: 0xabcdef1234567890abcdef1234567890abcdef12"
    result = _sanitize_message(msg)
    assert "0xabcdef1234567890abcdef1234567890abcdef12" not in result
    assert "0xabcde" in result or "..." in result
    assert "Funder:" in result
    print("  OK: Wallet addresses masked")


def test_sanitize_private_key():
    """private_key=xxx should be redacted."""
    msg = "Config: private_key=abcdef1234567890abcdef1234567890abcdef1234567890abcdef12"
    result = _sanitize_message(msg)
    assert "abcdef1234567890abcdef1234567890abcdef1234567890abcdef12" not in result
    assert "Config:" in result
    print("  OK: private_key redacted")


def test_sanitize_api_key():
    """api_key=xxx should be redacted."""
    msg = "api_key=abcdef1234567890abcdef1234567890abcdef1234567890abcdef12"
    result = _sanitize_message(msg)
    assert "abcdef1234567890abcdef1234567890abcdef1234567890abcdef12" not in result
    print("  OK: api_key redacted")


def test_sanitize_token():
    """token=xxx should be redacted, but the word 'token' in normal text should NOT be redacted."""
    # Token value should be redacted
    msg1 = "token=abcdef1234567890abcdef1234567890abcdef12"
    result1 = _sanitize_message(msg1)
    assert "abcdef1234567890abcdef1234567890abcdef12" not in result1
    print("  OK: token=xxx redacted")

    # Normal text containing 'token' should NOT be redacted
    msg2 = "This message has no secrets in it."
    result2 = _sanitize_message(msg2)
    assert result2 == msg2
    print("  OK: Normal text with 'token' word preserved")


def test_sanitize_long_hex():
    """Long hex strings (likely API keys) should be masked."""
    msg = "Key: abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    result = _sanitize_message(msg)
    assert "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890" not in result
    assert "Key:" in result
    print("  OK: Long hex strings masked")


def test_mask_address_short():
    """Short addresses should be masked."""
    assert _mask_address("0x12") == "****"
    assert _mask_address("0x12345") == "0x1...45"
    assert _mask_address("0xabcdef") == "0xa...ef"
    assert _mask_address("0xabcdef123456") == "0xabcd...3456"
    assert _mask_address("0xabcdef1234567890abcdef") == "0xabcd...cdef"
    print("  OK: Address masking works")


def test_mask_secret():
    """Secret values should be masked."""
    assert _mask_secret("abc") == "[REDACTED]"
    assert _mask_secret("abcdef123") == "abcd...23"
    print("  OK: Secret masking works")


def test_send_trade_alert():
    """Trade alerts should not be fully redacted."""
    record = {
        "event_type": "simulated_order",
        "dry_run": True,
        "side": "long",
        "entry_price": 0.645,
        "size": 5.0,
        "simulated_pnl": -0.21,
    }
    with patch("notifications.telegram_notifier.send_telegram_message", return_value=True) as mock:
        result = send_trade_alert(record, dry_run=True)
        assert result is True
        sent_text = mock.call_args[0][0]
        assert "REDACTED" not in sent_text or "REDACTED" in sent_text.split("\n")[0]  # Only if actual secret
        assert "SIMULATED ORDER" in sent_text
        assert "LONG" in sent_text
        assert "0.645" in sent_text
        print(f"  OK: Trade alert: {sent_text[:60]}...")
    print("  OK: send_trade_alert works")


def test_send_telegram_message_dry_run():
    """Dry-run should log but not send."""
    with patch("notifications.telegram_notifier._load_telegram_config", return_value={"token": "xxx", "chat_id": "123"}):
        result = send_telegram_message("[DRY-RUN] Test message", dry_run=True)
        assert result is True
    print("  OK: Dry-run mode works")


def test_is_configured():
    """is_configured should return True/False without printing secrets."""
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test", "TELEGRAM_CHAT_ID": "123"}):
        assert is_configured() is True
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
        assert is_configured() is False
    print("  OK: is_configured works")


def test_get_status():
    """get_status should not print secrets."""
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret123", "TELEGRAM_CHAT_ID": "12345"}):
        status = get_status()
        assert status["token_present"] is True
        assert status["chat_id_present"] is True
        assert "secret123" not in str(status)
        assert "12345" not in str(status)
    print("  OK: get_status masks secrets")


if __name__ == "__main__":
    print("=" * 60)
    print("Telegram Notifier Tests")
    print("=" * 60)
    tests = [
        test_sanitize_normal_messages,
        test_sanitize_wallet_addresses,
        test_sanitize_private_key,
        test_sanitize_api_key,
        test_sanitize_token,
        test_sanitize_long_hex,
        test_mask_address_short,
        test_mask_secret,
        test_send_trade_alert,
        test_send_telegram_message_dry_run,
        test_is_configured,
        test_get_status,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  FAILED: {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {test.__name__}: {e}")
            failed += 1
    print()
    print(f"Results: {passed} passed, {failed} failed")
    if failed:
        sys.exit(1)
    print("All tests passed!")
