"""
Macro Trend Diagnostics — BTC Daily Candles (Phase 1, diagnostics only)

Fetches BTC daily candles from public APIs (CoinGecko primary, Coinbase
fallback — NO Binance), caches them locally, and computes macro trend
features:

  - SMA/EMA 20D / 50D / 200D
  - price vs 50D / 200D
  - 20D / 50D / 200D slope (simple % change of the MA over a short lookback)
  - 50D vs 200D spread
  - macro_regime: macro_bull / macro_bear / macro_neutral / unavailable

Design rules:
  - NEVER raise to the caller. Any failure → macro_regime="unavailable".
  - NEVER fetch inside the hot decision loop. Candles are served from a
    local cache and only refreshed when the cache is older than
    `max_age_seconds` (default 12h).
  - Pure-compute helpers (sma/ema/slope) are import-safe and side-effect free.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_CACHE_FILE = "logs/btc_daily_candles.json"
_DEFAULT_MAX_AGE_SECONDS = 12 * 3600  # refresh at most twice a day
_MIN_CANDLES_FOR_200D = 200
_SLOPE_LOOKBACK_DAYS = 5  # compare MA[-1] vs MA[-1-5]

# Macro regime neutral band for the 50D-vs-200D spread (fraction of price).
_DEFAULT_SPREAD_NEUTRAL = 0.0


# ---------------------------------------------------------------------------
# Pure-compute helpers (no I/O)
# ---------------------------------------------------------------------------
def sma(values: List[float], period: int) -> Optional[float]:
    """Simple moving average of the last `period` values, or None."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def ema(values: List[float], period: int) -> Optional[float]:
    """Exponential moving average over the full series, or None."""
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    # Seed with SMA of the first `period` values.
    seed = sum(values[:period]) / period
    e = seed
    for v in values[period:]:
        e = v * k + e * (1.0 - k)
    return e


def ma_slope(values: List[float], period: int, lookback: int = _SLOPE_LOOKBACK_DAYS) -> Optional[float]:
    """
    Simple slope of an SMA: percentage change of SMA[now] vs SMA[lookback ago].

    Returns a fraction (e.g. 0.012 == +1.2% over the lookback window), or None
    if there isn't enough data.
    """
    if period <= 0 or lookback <= 0:
        return None
    if len(values) < period + lookback:
        return None
    now_ma = sma(values, period)
    past_ma = sma(values[:-lookback], period)
    if now_ma is None or past_ma is None or past_ma == 0:
        return None
    return (now_ma - past_ma) / past_ma


# ---------------------------------------------------------------------------
# Candle fetch + cache
# ---------------------------------------------------------------------------
def _cache_path() -> Path:
    return Path(os.path.abspath(_CACHE_FILE))


def _load_cache() -> Optional[Dict[str, Any]]:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("closes"):
            return data
    except Exception:
        return None
    return None


def _save_cache(closes: List[float], source: str) -> None:
    path = _cache_path()
    record = {
        "fetched_at": time.time(),
        "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "closes": [round(float(c), 2) for c in closes],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    except Exception:
        # Persistence is best-effort; never raise.
        pass


def _fetch_coingecko_daily() -> Optional[List[float]]:
    """Daily BTC closes from CoinGecko market_chart (last ~365 days)."""
    url = (
        "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
        "?vs_currency=usd&days=365&interval=daily"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    prices = data.get("prices") or []
    closes = [float(p[1]) for p in prices if isinstance(p, list) and len(p) == 2]
    return closes or None


def _fetch_coinbase_daily() -> Optional[List[float]]:
    """Daily BTC closes from Coinbase candles (max ~300 candles, oldest first)."""
    url = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400"
    req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())
    # Coinbase returns [time, low, high, open, close, volume], newest first.
    if not isinstance(data, list) or not data:
        return None
    rows = sorted(data, key=lambda r: r[0])  # oldest first
    closes = [float(r[4]) for r in rows if isinstance(r, list) and len(r) >= 5]
    return closes or None


def get_daily_closes(
    max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Return cached daily closes, refreshing from the network only when the
    cache is missing or older than `max_age_seconds`.

    Never raises. On total failure returns {"closes": [], "source": "none", ...}.
    """
    cache = _load_cache()
    now = time.time()

    fresh_enough = (
        cache is not None
        and not force_refresh
        and (now - float(cache.get("fetched_at", 0))) < max_age_seconds
    )
    if fresh_enough:
        return {
            "closes": cache["closes"],
            "source": cache.get("source", "cache"),
            "fetched_at": cache.get("fetched_at"),
            "from_cache": True,
            "error": None,
        }

    last_error: Optional[str] = None
    for name, fetcher in (("coingecko", _fetch_coingecko_daily), ("coinbase", _fetch_coinbase_daily)):
        try:
            closes = fetcher()
            if closes and len(closes) >= 20:
                _save_cache(closes, name)
                return {
                    "closes": [round(float(c), 2) for c in closes],
                    "source": name,
                    "fetched_at": now,
                    "from_cache": False,
                    "error": None,
                }
        except Exception as e:  # noqa: BLE001 — fail-safe by design
            last_error = f"{name}: {str(e)[:80]}"
            continue

    # Network failed — fall back to stale cache if we have one.
    if cache is not None and cache.get("closes"):
        return {
            "closes": cache["closes"],
            "source": cache.get("source", "stale_cache"),
            "fetched_at": cache.get("fetched_at"),
            "from_cache": True,
            "error": last_error or "refresh_failed_using_stale_cache",
        }

    return {
        "closes": [],
        "source": "none",
        "fetched_at": None,
        "from_cache": False,
        "error": last_error or "no_data",
    }


# ---------------------------------------------------------------------------
# Macro regime computation
# ---------------------------------------------------------------------------
def compute_macro_features(closes: List[float]) -> Dict[str, Any]:
    """
    Compute macro trend features and macro_regime from a list of daily closes
    (oldest first). Pure function — never raises, returns "unavailable" when
    there isn't enough history.
    """
    result: Dict[str, Any] = {
        "macro_regime": "unavailable",
        "macro_price": None,
        "sma_20d": None,
        "sma_50d": None,
        "sma_200d": None,
        "price_vs_50d": None,
        "price_vs_200d": None,
        "slope_20d": None,
        "slope_50d": None,
        "slope_200d": None,
        "spread_50_200": None,
        "macro_candle_count": len(closes) if closes else 0,
        "macro_error": None,
    }

    if not closes:
        result["macro_error"] = "no_candles"
        return result

    price = closes[-1]
    result["macro_price"] = round(price, 2)

    s20 = sma(closes, 20)
    s50 = sma(closes, 50)
    s200 = sma(closes, 200)
    result["sma_20d"] = round(s20, 2) if s20 is not None else None
    result["sma_50d"] = round(s50, 2) if s50 is not None else None
    result["sma_200d"] = round(s200, 2) if s200 is not None else None

    result["slope_20d"] = _round_or_none(ma_slope(closes, 20))
    result["slope_50d"] = _round_or_none(ma_slope(closes, 50))
    result["slope_200d"] = _round_or_none(ma_slope(closes, 200))

    if s50 is not None and s50 > 0:
        result["price_vs_50d"] = round((price - s50) / s50, 6)
    if s200 is not None and s200 > 0:
        result["price_vs_200d"] = round((price - s200) / s200, 6)
    if s50 is not None and s200 is not None and price > 0:
        result["spread_50_200"] = round((s50 - s200) / price, 6)

    # Need at least the 200D average to call a true macro regime.
    if s50 is None or s200 is None:
        result["macro_error"] = "insufficient_history_for_200d"
        return result

    above_50 = price > s50
    above_200 = price > s200
    golden = s50 > s200  # 50D above 200D (golden-cross-ish)

    if above_50 and above_200 and golden:
        result["macro_regime"] = "macro_bull"
    elif (not above_50) and (not above_200) and (not golden):
        result["macro_regime"] = "macro_bear"
    else:
        result["macro_regime"] = "macro_neutral"

    return result


def _round_or_none(v: Optional[float], digits: int = 6) -> Optional[float]:
    return round(v, digits) if v is not None else None


def get_macro_regime(
    max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Top-level macro diagnostic: load (cached) daily closes and compute the
    macro regime. Never raises.
    """
    try:
        candles = get_daily_closes(max_age_seconds=max_age_seconds, force_refresh=force_refresh)
        feats = compute_macro_features(candles.get("closes", []))
        feats["macro_source"] = candles.get("source", "none")
        feats["macro_from_cache"] = candles.get("from_cache", False)
        if feats.get("macro_error") is None and candles.get("error"):
            feats["macro_error"] = candles.get("error")
        return feats
    except Exception as e:  # noqa: BLE001 — fail-safe by design
        return {
            "macro_regime": "unavailable",
            "macro_source": "error",
            "macro_error": str(e)[:120],
            "macro_candle_count": 0,
        }
