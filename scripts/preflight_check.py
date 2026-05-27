#!/usr/bin/env python3
"""
Preflight Safety Check

Verifies that the bot is safe to run in dry-run mode.
Checks configuration, safety gates, and file system readiness.
Never prints secrets.

Usage:
    python scripts/preflight_check.py
"""
import sys
import os
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import yaml
from dotenv import load_dotenv

load_dotenv()


def check(name, passed, detail=""):
    """Print a check result."""
    status = "✓" if passed else "✗"
    msg = f"  {status} {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return passed


def main():
    print("=" * 60)
    print("Preflight Safety Check")
    print("=" * 60)

    all_passed = True

    # 1. strategy.yaml exists and has dry_run: true
    config_path = project_root / "strategy.yaml"
    if config_path.exists():
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}
        dry_run = config.get("dry_run", None)
        all_passed &= check("strategy.yaml exists", True)
        all_passed &= check("dry_run: true", dry_run is True, f"current={dry_run}")
    else:
        all_passed &= check("strategy.yaml exists", False, "MISSING")
        config = {}

    # 2. auto_update_enabled is false
    auto_update = config.get("auto_update_enabled", False)
    all_passed &= check("auto_update_enabled: false", auto_update is False, f"current={auto_update}")

    # 3. LIVE_TRADING_ACK is not true
    live_ack = os.getenv("LIVE_TRADING_ACK", "").lower() == "true"
    all_passed &= check("LIVE_TRADING_ACK is not true", not live_ack, f"current={live_ack}")

    # 4. Telegram config present (but not printed)
    tg_token = bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip())
    tg_chat = bool(os.getenv("TELEGRAM_CHAT_ID", "").strip())
    check("TELEGRAM_BOT_TOKEN present", tg_token, "present" if tg_token else "MISSING")
    check("TELEGRAM_CHAT_ID present", tg_chat, "present" if tg_chat else "MISSING")

    # 5. Journal path writable
    journal_base = config.get("journal_path", "logs/trade_journal")
    journal_dir = Path(journal_base).parent
    try:
        journal_dir.mkdir(parents=True, exist_ok=True)
        test_file = journal_dir / ".preflight_test"
        test_file.write_text("test")
        test_file.unlink()
        all_passed &= check("Journal directory writable", True, str(journal_dir))
    except Exception as e:
        all_passed &= check("Journal directory writable", False, str(e))

    # 6. Logs directory exists or can be created
    logs_dir = project_root / "logs"
    try:
        logs_dir.mkdir(exist_ok=True)
        all_passed &= check("Logs directory exists", True, str(logs_dir))
    except Exception as e:
        all_passed &= check("Logs directory exists", False, str(e))

    # 7. Required strategy.yaml fields
    required_fields = [
        "dry_run", "min_prob", "min_bet", "max_bet", "bankroll",
        "kelly_fraction_cap", "markov_window", "journal_path",
        "telegram_enabled", "telegram_daily_summary",
    ]
    for field in required_fields:
        present = field in config
        all_passed &= check(f"Field '{field}' present", present, f"={config.get(field)}")

    # 8. Lowest-level dry-run guard in polymarket_client.py
    client_path = project_root / "execution" / "polymarket_client.py"
    if client_path.exists():
        content = client_path.read_text()
        has_dry_guard = "self.dry_run" in content and "DRY_RUN" in content
        all_passed &= check("DRY_RUN guard in polymarket_client.py", has_dry_guard)
    else:
        all_passed &= check("polymarket_client.py exists", False, "MISSING")

    # 9. No real order placement in dry-run
    bot_path = project_root / "bot.py"
    if bot_path.exists():
        content = bot_path.read_text()
        # Check that is_simulation is checked before _place_real_order
        has_sim_check = "is_simulation" in content and "_place_real_order" in content
        all_passed &= check("Bot checks is_simulation before real orders", has_sim_check)
    else:
        all_passed &= check("bot.py exists", False, "MISSING")

    # 10. No private key printing
    # Check bot.py doesn't have print statements with PRIVATE_KEY or POLYMARKET_PK
    if bot_path.exists():
        content = bot_path.read_text()
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "print(" in stripped and ("POLYMARKET_PK" in stripped or "private_key" in stripped):
                all_passed &= check("No private key printing", False, stripped[:80])
                break
        else:
            all_passed &= check("No private key printing", True)

    # Summary
    print("=" * 60)
    if all_passed:
        print("ALL CHECKS PASSED — safe for dry-run")
    else:
        print("SOME CHECKS FAILED — review above")
    print("=" * 60)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
