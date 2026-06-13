"""
Regime Aggregator (Phase 1, diagnostics only)

Combines the three Phase-1 diagnostic layers into a single snapshot:

  - intraday : existing short-term tracker (15m/1h/4h returns) — unchanged
  - macro    : daily-candle trend regime (SMA 20/50/200, slopes, spread)
  - volatility : realized-volatility regime

The combined snapshot is persisted to logs/btc_regime_history.jsonl so that
actual simulated trades can later be joined to the nearest regime record.

HARD RULES:
  - This module NEVER affects trading. It only records diagnostics.
  - It NEVER raises to the caller. Any failure degrades to "unavailable".
  - Macro/volatility candles are served from a local cache and refreshed only
    when older than `macro_max_age_seconds` — NEVER fetched in the hot loop
    beyond that age check.

Phase 2 (NOT implemented): crypto breadth / TradingView BPI-style index.
A placeholder field `breadth_regime="unavailable"` is included so downstream
schemas are stable, but no breadth data is fetched.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from core.strategy_brain.btc_regime_tracker import get_btc_regime
from core.strategy_brain.regime.daily_candles import get_macro_regime, get_daily_closes
from core.strategy_brain.regime.volatility import compute_volatility_features

_SNAPSHOT_FILE = "logs/btc_regime_history.jsonl"
_DEFAULT_MACRO_MAX_AGE = 12 * 3600  # refresh daily candles at most twice/day
_DEFAULT_SNAPSHOT_INTERVAL = 300     # persist a snapshot at most every 5 min

# Module-level throttle for snapshot writes.
_last_snapshot_write: float = 0.0


def _snapshot_path() -> Path:
    return Path(os.path.abspath(_SNAPSHOT_FILE))


def _combined_regime(macro: str, intraday: str) -> str:
    """Stable string combining macro + intraday for easy grouping."""
    return f"{macro or 'unavailable'}|{intraday or 'unavailable'}"


def get_full_regime(
    macro_max_age_seconds: int = _DEFAULT_MACRO_MAX_AGE,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Build the combined diagnostic regime snapshot.

    Returns a superset of the legacy `get_btc_regime()` dict, so existing
    callers/consumers keep working unchanged. Never raises.
    """
    now = time.time()
    snapshot: Dict[str, Any] = {
        "ts": round(now, 2),
        "snapshot_timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # --- Intraday (existing tracker, unchanged behavior) ---
    try:
        intraday = get_btc_regime()
    except Exception as e:  # noqa: BLE001 — fail-safe
        intraday = {
            "btc_price": None, "btc_return_15m": None, "btc_return_1h": None,
            "btc_return_4h": None, "btc_regime": "unavailable",
            "intraday_regime": "unavailable", "regime_source": "error",
            "regime_timestamp": datetime.now(timezone.utc).isoformat(),
            "regime_error": str(e)[:80],
        }
    snapshot.update(intraday)
    # Ensure intraday_regime is always present (older tracker code paths).
    snapshot.setdefault("intraday_regime", snapshot.get("btc_regime", "unavailable"))

    # --- Macro (daily candles, cached) ---
    try:
        macro = get_macro_regime(max_age_seconds=macro_max_age_seconds, force_refresh=force_refresh)
    except Exception as e:  # noqa: BLE001 — fail-safe
        macro = {"macro_regime": "unavailable", "macro_error": str(e)[:80]}
    snapshot.update(macro)

    # --- Volatility (reuse the macro candle cache; no extra network call) ---
    try:
        closes = get_daily_closes(max_age_seconds=macro_max_age_seconds).get("closes", [])
        vol = compute_volatility_features(closes)
    except Exception as e:  # noqa: BLE001 — fail-safe
        vol = {"volatility_regime": "unavailable", "vol_error": str(e)[:80]}
    snapshot.update(vol)

    # --- Phase 2 placeholder (no fetch) ---
    snapshot.setdefault("breadth_regime", "unavailable")
    snapshot.setdefault("breadth_note", "phase2_not_implemented")

    # --- Combined regime label ---
    snapshot["combined_regime"] = _combined_regime(
        snapshot.get("macro_regime", "unavailable"),
        snapshot.get("intraday_regime", "unavailable"),
    )

    return snapshot


def persist_snapshot(
    snapshot: Dict[str, Any],
    min_interval_seconds: int = _DEFAULT_SNAPSHOT_INTERVAL,
) -> bool:
    """
    Append a compact regime snapshot to logs/btc_regime_history.jsonl.

    Rate-limited to one write per `min_interval_seconds`. Returns True if a
    record was written. Never raises.
    """
    global _last_snapshot_write
    now = time.time()
    if (now - _last_snapshot_write) < min_interval_seconds:
        return False

    record = {
        "ts": snapshot.get("ts", round(now, 2)),
        "t": snapshot.get("snapshot_timestamp"),
        "btc_price": snapshot.get("btc_price"),
        # intraday
        "intraday_regime": snapshot.get("intraday_regime", "unavailable"),
        "btc_return_15m": snapshot.get("btc_return_15m"),
        "btc_return_1h": snapshot.get("btc_return_1h"),
        "btc_return_4h": snapshot.get("btc_return_4h"),
        # macro
        "macro_regime": snapshot.get("macro_regime", "unavailable"),
        "sma_50d": snapshot.get("sma_50d"),
        "sma_200d": snapshot.get("sma_200d"),
        "price_vs_50d": snapshot.get("price_vs_50d"),
        "price_vs_200d": snapshot.get("price_vs_200d"),
        "slope_50d": snapshot.get("slope_50d"),
        "slope_200d": snapshot.get("slope_200d"),
        "spread_50_200": snapshot.get("spread_50_200"),
        # volatility
        "volatility_regime": snapshot.get("volatility_regime", "unavailable"),
        "realized_vol": snapshot.get("realized_vol"),
        "vol_percentile": snapshot.get("vol_percentile"),
        # combined + phase2 placeholder
        "combined_regime": snapshot.get("combined_regime", "unavailable|unavailable"),
        "breadth_regime": snapshot.get("breadth_regime", "unavailable"),
        # provenance
        "intraday_source": snapshot.get("regime_source"),
        "macro_source": snapshot.get("macro_source"),
    }

    try:
        path = _snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
        _last_snapshot_write = now
        return True
    except Exception:
        # Persistence is best-effort; never fail the bot.
        return False


def get_and_persist_regime(
    macro_max_age_seconds: int = _DEFAULT_MACRO_MAX_AGE,
    snapshot_interval_seconds: int = _DEFAULT_SNAPSHOT_INTERVAL,
) -> Dict[str, Any]:
    """
    Convenience entry point for the bot: build the combined regime snapshot,
    persist it (rate-limited), and return the full dict. Never raises.
    """
    snapshot = get_full_regime(macro_max_age_seconds=macro_max_age_seconds)
    try:
        persist_snapshot(snapshot, min_interval_seconds=snapshot_interval_seconds)
    except Exception:
        pass
    return snapshot
