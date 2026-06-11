"""
BTC Regime Tracker — Diagnostic Only

Fetches BTC spot price from multiple public APIs with fallback.
Persists price history to disk so it survives bot restarts.

DOES NOT affect trading decisions. Only records diagnostic data.

Features:
  - Price history persisted to logs/btc_price_history.jsonl
  - History loaded on startup (last 6 hours of prices)
  - Duplicate writes avoided if price unchanged
  - 15m/1h/4h returns computed from persisted history
  - Provider fallback: CoinGecko → Coinbase → CoinCap
  - Fail-safe: all fields set to null/unavailable on error

Regime thresholds (configurable):
  bullish: 1h return > +0.3%
  bearish: 1h return < -0.3%
  neutral: otherwise
"""
import json
import os
import time
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any


_DEFAULT_BULLISH_THRESHOLD = 0.003
_DEFAULT_BEARISH_THRESHOLD = -0.003
_HISTORY_FILE = "logs/btc_price_history.jsonl"

# Keep 6 hours of prices (21600 seconds / 60 second poll = 360 entries max)
_HISTORY_MAX_AGE_SECONDS = 6 * 3600  # 6 hours
_HISTORY_WRITE_INTERVAL = 60  # Write to file at most every 60 seconds


class BTCRegimeTracker:
    """
    BTC regime tracker with spot price polling, local rolling history,
    and file-based persistence across bot restarts.
    """

    def __init__(
        self,
        bullish_threshold: float = _DEFAULT_BULLISH_THRESHOLD,
        bearish_threshold: float = _DEFAULT_BEARISH_THRESHOLD,
        cache_seconds: int = 60,
    ):
        self.bullish_threshold = bullish_threshold
        self.bearish_threshold = bearish_threshold
        self.cache_seconds = cache_seconds
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_time: float = 0
        self._last_write_time: float = 0
        self._price_history: deque = deque(maxlen=2000)  # generous max
        # Use absolute path to avoid CWD issues
        self._history_path = Path(os.path.abspath(_HISTORY_FILE))

        # Load persisted history on startup
        self._load_history()

    def _load_history(self):
        """Load recent price history from disk (last 6 hours)."""
        if not self._history_path.exists():
            return

        cutoff = time.time() - _HISTORY_MAX_AGE_SECONDS
        loaded = 0
        try:
            with open(self._history_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ts = rec.get("ts", 0)
                        price = rec.get("price", 0)
                        if ts > cutoff and price > 0:
                            self._price_history.append((ts, price))
                            loaded += 1
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            print(f"[BTCRegime] Error loading history: {e}")
        if loaded > 0:
            print(f"[BTCRegime] Loaded {loaded} price records from history file")

    def _persist_price(self, timestamp: float, price: float, source: str):
        """Append price to history file (rate-limited to every 60s)."""
        now = time.time()
        if (now - self._last_write_time) < _HISTORY_WRITE_INTERVAL:
            return

        # Avoid duplicate writes (skip if last price is the same)
        if self._price_history:
            last_ts, last_price = self._price_history[-1]
            if abs(last_price - price) < 0.01 and (timestamp - last_ts) < 120:
                return

        record = {
            "ts": round(timestamp, 2),
            "price": round(price, 2),
            "source": source,
            "t": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(),
        }

        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._history_path, "a") as f:
                f.write(json.dumps(record) + "\n")
            self._last_write_time = now
        except Exception as e:
            print(f"[BTCRegime] Error persisting price: {e}")

    def _trim_history(self):
        """Remove entries older than 6 hours."""
        cutoff = time.time() - _HISTORY_MAX_AGE_SECONDS
        while self._price_history and self._price_history[0][0] < cutoff:
            self._price_history.popleft()

    def get_regime(self) -> Dict[str, Any]:
        """Get current BTC regime with fallback providers and rolling history."""
        now = time.time()
        if self._cache and (now - self._cache_time) < self.cache_seconds:
            return self._cache

        result = {
            "btc_price": None,
            "btc_return_15m": None,
            "btc_return_1h": None,
            "btc_return_4h": None,
            "btc_regime": "unavailable",
            "regime_source": "none",
            "regime_timestamp": datetime.now(timezone.utc).isoformat(),
            "regime_error": None,
            "btc_history_points": len(self._price_history),
            "btc_history_span_minutes": 0,
        }

        # Fetch current spot price from fallback providers
        providers = [
            ("coingecko", self._fetch_coingecko_price),
            ("coinbase", self._fetch_coinbase_price),
            ("coincap", self._fetch_coincap_price),
        ]

        current_price = None
        for name, fetcher in providers:
            try:
                price = fetcher()
                if price is not None and price > 0:
                    current_price = price
                    result["regime_source"] = name
                    result["regime_error"] = None
                    break
            except Exception as e:
                result["regime_error"] = f"{name}: {str(e)[:80]}"
                continue

        if current_price is None:
            result["regime_error"] = result.get("regime_error", "all_providers_failed")
            self._cache = result
            self._cache_time = now
            return result

        # Persist first, then add to rolling history
        self._persist_price(now, current_price, result["regime_source"])
        self._price_history.append((now, current_price))
        self._trim_history()

        result["btc_price"] = current_price
        result["btc_history_points"] = len(self._price_history)

        # Compute history span
        if len(self._price_history) >= 2:
            span = self._price_history[-1][0] - self._price_history[0][0]
            result["btc_history_span_minutes"] = round(span / 60, 1)

        # Compute returns from rolling history
        history = list(self._price_history)
        if len(history) >= 2:
            # 15m return
            target_15m = now - 900
            old_15m = self._find_closest_price(history, target_15m)
            if old_15m is not None and old_15m > 0:
                result["btc_return_15m"] = round((current_price - old_15m) / old_15m, 6)
            else:
                result["regime_error"] = "insufficient_history_15m"

            # 1h return
            target_1h = now - 3600
            old_1h = self._find_closest_price(history, target_1h)
            if old_1h is not None and old_1h > 0:
                result["btc_return_1h"] = round((current_price - old_1h) / old_1h, 6)
            else:
                if result["regime_error"] is None:
                    result["regime_error"] = "insufficient_history_1h"

            # 4h return
            target_4h = now - 14400
            old_4h = self._find_closest_price(history, target_4h)
            if old_4h is not None and old_4h > 0:
                result["btc_return_4h"] = round((current_price - old_4h) / old_4h, 6)
            else:
                if result["regime_error"] is None:
                    result["regime_error"] = "insufficient_history_4h"

        # Regime classification (use 1h return as primary)
        ret_1h = result["btc_return_1h"]
        if ret_1h is not None:
            if ret_1h > self.bullish_threshold:
                result["btc_regime"] = "bullish"
            elif ret_1h < self.bearish_threshold:
                result["btc_regime"] = "bearish"
            else:
                result["btc_regime"] = "neutral"
        else:
            result["btc_regime"] = "unavailable"
            if result["regime_error"] is None:
                result["regime_error"] = "insufficient_history"

        self._cache = result
        self._cache_time = now
        return result

    @staticmethod
    def _find_closest_price(history: list, target_time: float) -> Optional[float]:
        """Find the price closest to target_time in history."""
        if not history:
            return None
        best = None
        best_diff = float("inf")
        for ts, price in history:
            diff = abs(ts - target_time)
            if diff < best_diff:
                best_diff = diff
                best = price
        return best

    def _fetch_coingecko_price(self) -> Optional[float]:
        """Fetch current BTC price from CoinGecko."""
        url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
        req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return float(data["bitcoin"]["usd"])

    def _fetch_coinbase_price(self) -> Optional[float]:
        """Fetch current BTC price from Coinbase."""
        url = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
        req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return float(data["data"]["amount"])

    def _fetch_coincap_price(self) -> Optional[float]:
        """Fetch current BTC price from CoinCap."""
        url = "https://api.coincap.io/v2/assets/bitcoin"
        req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return float(data["data"]["priceUsd"])


# Singleton
_tracker: Optional[BTCRegimeTracker] = None


def get_btc_regime_tracker() -> BTCRegimeTracker:
    """Get or create singleton BTC regime tracker."""
    global _tracker
    if _tracker is None:
        _tracker = BTCRegimeTracker()
    return _tracker


def get_btc_regime() -> Dict[str, Any]:
    """Convenience function to get current BTC regime."""
    return get_btc_regime_tracker().get_regime()
