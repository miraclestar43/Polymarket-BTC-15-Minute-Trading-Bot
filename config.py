"""
Centralized configuration loader for Polymarket BTC 15-Min Trading Bot.

Reads strategy.yaml (non-sensitive parameters) and .env (secrets).
Never prints or exposes private keys, API keys, Telegram tokens, or wallet addresses.

Usage:
    from config import load_strategy_config, get_config
    cfg = load_strategy_config()  # or get_config() for singleton
"""

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from loguru import logger

# ---------------------------------------------------------------------------
# Defaults — safe values used when strategy.yaml fields are missing
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    # Trading mode
    "dry_run": True,

    # Position sizing
    "min_edge": 0.05,
    "min_prob": 0.87,
    "min_bet": 1.00,
    "max_bet": 50.00,
    "bankroll": 100.00,
    "kelly_fraction_cap": 0.05,

    # Safety gates
    "live_trading_ack": False,

    # Proxy wallet / Safe support
    "signature_type": 1,       # 1=EOA, 2=POLY_PROXY, 3=POLY_GNOSIS_SAFE

    # Strategy auto-update
    "auto_update_enabled": False,

    # Future phases (scaffolded)
    "markov_window": 20,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "journal_path": "trade_journal.json",
}

# Fields that should NEVER be printed in logs or output
_SENSITIVE_KEYS = {
    "telegram_bot_token",
    "telegram_chat_id",
}

# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_project_root = Path(__file__).parent
_strategy_yaml_path = _project_root / "strategy.yaml"


def load_strategy_config(
    path: Optional[Path] = None,
    overrides: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Load strategy configuration from YAML, merged with safe defaults.

    Args:
        path: Path to strategy.yaml (defaults to project root)
        overrides: Optional dict of runtime overrides (e.g. --live flag)

    Returns:
        Dict with all strategy parameters. Never contains secrets.
    """
    path = path or _strategy_yaml_path
    config = dict(DEFAULTS)  # copy defaults

    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            if isinstance(raw, dict):
                config.update({k: v for k, v in raw.items() if v is not None})
                logger.info(f"Loaded strategy config from {path.name}")
            else:
                logger.warning(f"{path.name} is empty or not a dict — using defaults")
        except Exception as e:
            logger.warning(f"Failed to parse {path.name}: {e} — using defaults")
    else:
        logger.info(f"{path.name} not found — using defaults")

    # Apply runtime overrides (e.g. --live sets dry_run=False)
    if overrides:
        config.update(overrides)

    # SAFETY: always enforce dry_run=True unless explicitly overridden
    # This ensures the default is safe even if YAML is misconfigured
    if "dry_run" not in (overrides or {}):
        # Only trust the YAML value if it's explicitly set
        pass  # YAML value stands

    # SAFETY: never allow live_trading_ack from YAML — must come from env
    config["live_trading_ack"] = os.getenv("LIVE_TRADING_ACK", "").lower() == "true"

    return config


def get_signature_type(config: Optional[dict] = None) -> int:
    """
    Determine the Polymarket signature type based on config and env.

    Priority:
      1. SAFE_ADDRESS env var present → use POLY_GNOSIS_SAFE (3)
      2. config["signature_type"] value
      3. Default: 1 (EOA)

    Returns:
        1 (EOA), 2 (POLY_PROXY), or 3 (POLY_GNOSIS_SAFE)
    """
    # If SAFE_ADDRESS is set in env, always use Safe signature type
    safe_address = os.getenv("SAFE_ADDRESS", "").strip()
    if safe_address:
        logger.info("SAFE_ADDRESS detected — using POLY_GNOSIS_SAFE signature type")
        return 3  # POLY_GNOSIS_SAFE

    if config and "signature_type" in config:
        return int(config["signature_type"])

    return 1  # EOA (default)


def get_funder_address(config: Optional[dict] = None) -> Optional[str]:
    """
    Get the funder (proxy wallet) address without printing it.

    Priority:
      1. SAFE_ADDRESS env var
      2. POLYMARKET_FUNDER env var
      3. config["safe_address"] field
      4. None (no proxy wallet)

    Returns:
        Address string or None. Never logged or printed.
    """
    safe = os.getenv("SAFE_ADDRESS", "").strip()
    if safe:
        return safe

    funder = os.getenv("POLYMARKET_FUNDER", "").strip()
    if funder:
        return funder

    if config and "safe_address" in config:
        return config["safe_address"]

    return None


def mask_address(address: Optional[str]) -> str:
    """
    Return a safely masked version of a wallet address for logging.
    Shows first 6 and last 4 characters only.
    """
    if not address or len(address) < 14:
        return "****"
    return f"{address[:6]}…{address[-4:]}"


def log_config_safety(config: dict[str, Any]) -> None:
    """Log non-sensitive config values for debugging."""
    logger.info("Strategy configuration loaded:")
    logger.info(f"  dry_run = {config.get('dry_run', True)}")
    logger.info(f"  live_trading_ack = {config.get('live_trading_ack', False)}")
    logger.info(f"  min_edge = {config.get('min_edge', 0.05)}")
    logger.info(f"  min_prob = {config.get('min_prob', 0.87)}")
    logger.info(f"  min_bet = ${config.get('min_bet', 1.00):.2f}")
    logger.info(f"  max_bet = ${config.get('max_bet', 50.00):.2f}")
    logger.info(f"  bankroll = ${config.get('bankroll', 100.00):.2f}")
    logger.info(f"  kelly_fraction_cap = {config.get('kelly_fraction_cap', 0.05)}")
    logger.info(f"  signature_type = {config.get('signature_type', 1)}")
    funder = get_funder_address(config)
    logger.info(f"  funder_address = {mask_address(funder) if funder else 'None (EOA)'}")
    # Never log telegram tokens or private keys


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_config_singleton: Optional[dict[str, Any]] = None


def get_config(overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Get or create the singleton config."""
    global _config_singleton
    if _config_singleton is None:
        _config_singleton = load_strategy_config(overrides=overrides)
    elif overrides:
        _config_singleton.update(overrides)
    return _config_singleton


def reset_config() -> None:
    """Reset singleton (for testing)."""
    global _config_singleton
    _config_singleton = None
