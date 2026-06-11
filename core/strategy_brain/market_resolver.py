"""
Market Resolution Tracker — Diagnostic Only

Queries Polymarket/Gamma API for market resolution outcomes.
Enables matching shadow candidates to actual market results.

DOES NOT affect trading decisions. Only records diagnostic data.

Features:
  - Resolves markets by condition_id (extracted from NautilusTrader ID)
  - Persistent resolution cache in logs/market_resolutions_cache.json
  - Handles NautilusTrader market_id format: 0x...-0x....POLYMARKET

Usage:
    resolver = MarketResolver()
    result = resolver.resolve(market_id="0x...-0x....POLYMARKET")
"""
import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any


_GAMMA_API_BASE = "https://gamma-api.polymarket.com"
_RESOLUTION_CACHE_FILE = "logs/market_resolutions_cache.json"


class MarketResolver:
    """
    Resolves Polymarket market outcomes using Gamma API.
    Diagnostic only — does not affect trading decisions.
    """

    def __init__(self, cache_seconds: int = 300):
        self.cache_seconds = cache_seconds
        self._api_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_path = Path(os.path.abspath(_RESOLUTION_CACHE_FILE))
        self._disk_cache = self._load_disk_cache()

    def _load_disk_cache(self) -> Dict[str, Any]:
        """Load resolution cache from disk."""
        if not self._cache_path.exists():
            return {}
        try:
            with open(self._cache_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_disk_cache(self):
        """Save resolution cache to disk."""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._cache_path, "w") as f:
                json.dump(self._disk_cache, f, indent=2)
        except Exception as e:
            print(f"[MarketResolver] Error saving cache: {e}")

    @staticmethod
    def _extract_condition_id(market_id: str) -> Optional[str]:
        """
        Extract condition_id from NautilusTrader market_id format.

        NautilusTrader format: 0x{condition_id}-{token_id}.POLYMARKET
        Gamma API uses condition_id.
        """
        if not market_id:
            return None

        # Handle full NautilusTrader format
        if ".POLYMARKET" in market_id:
            parts = market_id.split("-")
            if len(parts) >= 2:
                condition_id = parts[0]
                if condition_id.startswith("0x") and len(condition_id) == 66:
                    return condition_id

        # Handle raw condition_id (0x + 64 hex chars)
        if market_id.startswith("0x") and len(market_id) == 66:
            return market_id

        # Handle slug format
        if not market_id.startswith("0x"):
            return None  # It's a slug, not a condition_id

        return None

    def resolve(
        self,
        market_id: Optional[str] = None,
        condition_id: Optional[str] = None,
        slug: Optional[str] = None,
        market_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Resolve a market outcome.

        Returns dict with:
          - resolution_status: pending / resolved / unavailable / error
          - resolved_outcome: YES / NO / UP / DOWN (if resolved)
          - resolution_time: ISO timestamp
          - resolution_source: "gamma_api"
          - resolution_error: error message (if any)
          - market_data: raw market data (if available)
        """
        # Extract condition_id from market_id if needed
        if market_id and not condition_id:
            condition_id = self._extract_condition_id(market_id)

        # Build cache key
        cache_key = condition_id or market_id or slug or "unknown"

        # Check disk cache first
        if cache_key in self._disk_cache:
            cached = self._disk_cache[cache_key]
            if (time.time() - cached.get("_cache_time", 0)) < self.cache_seconds:
                return cached

        # Check API cache
        if cache_key in self._api_cache:
            cached = self._api_cache[cache_key]
            if (time.time() - cached.get("_cache_time", 0)) < self.cache_seconds:
                return cached

        result = {
            "resolution_status": "unavailable",
            "resolved_outcome": None,
            "resolution_time": None,
            "resolution_source": "gamma_api",
            "resolution_error": None,
            "market_data": None,
        }

        try:
            # Use provided market_data if available, otherwise query Gamma API
            if market_data is None:
                market_data = self._fetch_market(
                    condition_id=condition_id,
                    slug=slug,
                )

            if market_data is None:
                result["resolution_status"] = "unavailable"
                result["resolution_error"] = "market_not_found"
                self._api_cache[cache_key] = result
                result["_cache_time"] = time.time()
                return result

            result["market_data"] = market_data

            # Check if market is resolved
            is_closed = market_data.get("closed", False)
            is_resolved = market_data.get("resolved", False)
            outcome = market_data.get("outcome")
            winner = market_data.get("winner")

            if is_resolved and outcome:
                result["resolution_status"] = "resolved"
                result["resolved_outcome"] = outcome.upper() if isinstance(outcome, str) else str(outcome)
                result["resolution_time"] = market_data.get("endDate") or market_data.get("updatedAt")
            elif is_closed:
                result["resolution_status"] = "resolved"
                # Try to extract outcome from tokens
                tokens = market_data.get("tokens", [])
                if tokens:
                    for token in tokens:
                        if token.get("winner"):
                            result["resolved_outcome"] = token.get("outcome", "").upper()
                            break
                if result["resolved_outcome"] is None:
                    result["resolution_status"] = "pending"
                    result["resolution_error"] = "closed_but_no_outcome"
            else:
                # Check for stale pending (market should have ended)
                end_date = market_data.get("endDate") or market_data.get("endDateIso")
                if end_date:
                    try:
                        from datetime import datetime as dt
                        end_time = dt.fromisoformat(end_date.replace("Z", "+00:00"))
                        age_minutes = (dt.now(end_time.tzinfo) - end_time).total_seconds() / 60
                        if age_minutes > 60:
                            result["resolution_status"] = "stale_pending"
                            result["resolution_error"] = f"market_ended_{age_minutes:.0f}_minutes_ago"
                    except Exception:
                        pass
                if result["resolution_status"] == "pending":
                    pass  # Keep as pending

        except Exception as e:
            result["resolution_status"] = "error"
            result["resolution_error"] = str(e)[:200]

        # Cache result
        result["_cache_time"] = time.time()
        self._api_cache[cache_key] = result
        self._disk_cache[cache_key] = result
        self._save_disk_cache()

        return result

    def _fetch_market(
        self,
        condition_id: Optional[str] = None,
        slug: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Fetch market data from Gamma API."""
        if condition_id:
            url = f"{_GAMMA_API_BASE}/markets?condition_id={condition_id}"
        elif slug:
            url = f"{_GAMMA_API_BASE}/markets?slug={slug}"
        else:
            return None

        req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

            # Handle list vs single market response
            if isinstance(data, list):
                return data[0] if data else None
            return data


# Singleton
_resolver: Optional[MarketResolver] = None


def get_market_resolver() -> MarketResolver:
    """Get or create singleton market resolver."""
    global _resolver
    if _resolver is None:
        _resolver = MarketResolver()
    return _resolver
