"""
Volatility Diagnostics (Phase 1, diagnostics only)

Computes a simple realized-volatility regime from BTC daily closes. Kept
deliberately simple and fail-safe:

  - realized_vol: stdev of the last N daily simple returns (default 30)
  - vol_percentile: percentile rank of the current 30-day realized vol vs the
    rolling history of 30-day realized vol over the available candles
  - volatility_regime: low / normal / high / unavailable

NEVER raises. Insufficient data → volatility_regime="unavailable".
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

_DEFAULT_WINDOW = 30
_LOW_PCT = 0.33
_HIGH_PCT = 0.66


def _daily_returns(closes: List[float]) -> List[float]:
    rets: List[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev and prev > 0:
            rets.append((cur - prev) / prev)
    return rets


def _stdev(values: List[float]) -> Optional[float]:
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return var ** 0.5


def _rolling_vol_series(returns: List[float], window: int) -> List[float]:
    series: List[float] = []
    if len(returns) < window:
        return series
    for i in range(window, len(returns) + 1):
        s = _stdev(returns[i - window:i])
        if s is not None:
            series.append(s)
    return series


def _percentile_rank(value: float, distribution: List[float]) -> Optional[float]:
    if not distribution:
        return None
    below = sum(1 for d in distribution if d < value)
    return below / len(distribution)


def compute_volatility_features(
    closes: List[float],
    window: int = _DEFAULT_WINDOW,
) -> Dict[str, Any]:
    """
    Pure function — compute realized volatility regime from daily closes
    (oldest first). Never raises.
    """
    result: Dict[str, Any] = {
        "volatility_regime": "unavailable",
        "realized_vol": None,
        "vol_window_days": window,
        "vol_percentile": None,
        "vol_error": None,
    }

    if not closes:
        result["vol_error"] = "no_candles"
        return result

    returns = _daily_returns(closes)
    if len(returns) < window:
        result["vol_error"] = "insufficient_history"
        return result

    current_vol = _stdev(returns[-window:])
    if current_vol is None:
        result["vol_error"] = "stdev_failed"
        return result
    result["realized_vol"] = round(current_vol, 6)

    distribution = _rolling_vol_series(returns, window)
    pct = _percentile_rank(current_vol, distribution) if distribution else None
    result["vol_percentile"] = round(pct, 4) if pct is not None else None

    if pct is None:
        # Not enough history for a distribution — report the value but no regime.
        result["vol_error"] = "insufficient_distribution"
        return result

    if pct < _LOW_PCT:
        result["volatility_regime"] = "low"
    elif pct >= _HIGH_PCT:
        result["volatility_regime"] = "high"
    else:
        result["volatility_regime"] = "normal"

    return result
