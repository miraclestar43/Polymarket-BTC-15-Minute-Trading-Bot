#!/usr/bin/env python3
"""
Send Daily Telegram Summary

Loads today's journal summary and sends it via Telegram.
Safe for dry-run mode — does not modify strategy parameters.

Usage:
    python scripts/send_daily_telegram_summary.py
    python scripts/send_daily_telegram_summary.py --date 2026-05-27
    python scripts/send_daily_telegram_summary.py --dry-run
"""
import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from journal.trade_journal import daily_summary
from notifications.telegram_notifier import (
    send_daily_summary,
    is_configured,
    get_status,
)
from dotenv import load_dotenv

load_dotenv()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Send daily journal summary via Telegram")
    parser.add_argument("--date", help="Date in YYYY-MM-DD format (default: today UTC)")
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
        print("ERROR: Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        sys.exit(1)

    # Load summary
    summary = daily_summary(date_str=args.date)

    if summary["total_records"] == 0:
        print(f"No journal records found for {summary['date']}")
        print("Nothing to send.")
        return

    # Print summary to stdout
    print(f"Daily Summary for {summary['date']}:")
    print(f"  Decisions: {summary['decisions']}")
    print(f"  Simulated orders: {summary['simulated_orders']}")
    print(f"  Skipped trades: {summary['skipped_trades']}")
    print(f"  Markov blocked: {summary['markov_blocked']}")
    print(f"  Kelly blocked: {summary['kelly_blocked']}")
    if summary.get("avg_p_stay") is not None:
        print(f"  Avg p_stay: {summary['avg_p_stay']:.4f}")
    if summary.get("avg_p_model") is not None:
        print(f"  Avg p_model: {summary['avg_p_model']:.4f}")
    if summary.get("avg_kelly_size") is not None:
        print(f"  Avg Kelly size: ${summary['avg_kelly_size']:.2f}")
    if summary.get("total_simulated_pnl") is not None:
        print(f"  Simulated P&L: ${summary['total_simulated_pnl']:+.2f}")

    # Send via Telegram
    success = send_daily_summary(summary, dry_run=args.dry_run)
    if success:
        print("\n✓ Summary sent successfully")
    else:
        print("\n✗ Failed to send summary")
        sys.exit(1)


if __name__ == "__main__":
    main()
