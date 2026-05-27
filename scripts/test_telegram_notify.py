#!/usr/bin/env python3
"""
Test Telegram Notifier

Sends a simple test message to verify the Telegram bot connection.
Does NOT place trades, print secrets, or read .env values.

Usage:
    python scripts/test_telegram_notify.py
    python scripts/test_telegram_notify.py --dry-run
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from notifications.telegram_notifier import (
    send_telegram_message,
    is_configured,
    get_status,
)
from dotenv import load_dotenv

load_dotenv()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Test Telegram notifier connection")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log message but don't actually send")
    parser.add_argument("--status", action="store_true",
                        help="Show Telegram config status (no secrets)")
    args = parser.parse_args()

    if args.status:
        status = get_status()
        print("Telegram Configuration Status:")
        print(f"  Token present: {status['token_present']}")
        print(f"  Chat ID present: {status['chat_id_present']}")
        print(f"  Configured: {status['configured']}")
        print(f"  Chat ID (masked): {status['chat_id_masked']}")
        return

    # Check config
    if not is_configured() and not args.dry_run:
        print("ERROR: Telegram not configured.")
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        sys.exit(1)

    # Send test message
    test_message = "Polymarket bot Telegram notifier test — dry-run system connected."
    print(f"Sending test message...")
    print(f"  (Token and Chat ID are NOT displayed)")

    success = send_telegram_message(test_message, dry_run=args.dry_run)

    if success:
        print("✓ Test message sent successfully")
    else:
        print("✗ Failed to send test message")
        sys.exit(1)


if __name__ == "__main__":
    main()
