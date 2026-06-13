"""
Phase 1 regime diagnostics — volatility computation tests.

Offline, deterministic, no network.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.strategy_brain.regime.volatility import compute_volatility_features


def test_volatility_unavailable_no_candles():
    out = compute_volatility_features([])
    assert out["volatility_regime"] == "unavailable"
    assert out["vol_error"] == "no_candles"


def test_volatility_unavailable_short_history():
    out = compute_volatility_features([100.0 + i for i in range(10)], window=30)
    assert out["volatility_regime"] == "unavailable"
    assert out["vol_error"] == "insufficient_history"


def test_volatility_high_vs_low_classification():
    # Build a long series: calm first half, turbulent second half. The current
    # 30-day window (turbulent) should rank HIGH vs the rolling distribution.
    calm = []
    price = 100.0
    for i in range(200):
        price *= 1.0005 if i % 2 == 0 else 0.9995  # tiny moves
        calm.append(price)
    turbulent = []
    for i in range(40):
        price *= 1.05 if i % 2 == 0 else 0.95  # large moves
        turbulent.append(price)
    out = compute_volatility_features(calm + turbulent, window=30)
    assert out["realized_vol"] is not None
    assert out["vol_percentile"] is not None
    assert out["volatility_regime"] == "high"


def test_volatility_regime_is_valid_label():
    out = compute_volatility_features([100.0 + (i % 5) for i in range(120)], window=30)
    assert out["volatility_regime"] in ("low", "normal", "high", "unavailable")
