"""
Phase 1 regime diagnostics — macro trend computation tests.

Offline, deterministic, no network. Proves SMA/EMA/slope math and
macro_regime classification on synthetic daily-close series.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.strategy_brain.regime.daily_candles import (
    sma,
    ema,
    ma_slope,
    compute_macro_features,
)


def test_sma_basic():
    assert sma([1, 2, 3, 4, 5], 5) == 3.0
    assert sma([1, 2, 3, 4, 5], 2) == 4.5
    assert sma([1, 2], 5) is None
    assert sma([], 3) is None


def test_ema_basic():
    # EMA of a flat series equals the constant.
    assert ema([10.0] * 50, 20) == 10.0
    # EMA returns None when not enough data.
    assert ema([1, 2, 3], 20) is None


def test_ma_slope_rising_and_falling():
    rising = list(range(1, 100))  # strictly increasing
    falling = list(range(100, 1, -1))
    s_up = ma_slope(rising, 20, lookback=5)
    s_down = ma_slope(falling, 20, lookback=5)
    assert s_up is not None and s_up > 0
    assert s_down is not None and s_down < 0
    # Not enough data -> None.
    assert ma_slope([1, 2, 3], 20) is None


def _series_bull(n=260):
    # Steadily rising series: price > 50D > 200D.
    return [100.0 + i for i in range(n)]


def _series_bear(n=260):
    # Steadily falling series: price < 50D < 200D.
    return [1000.0 - i for i in range(n)]


def test_macro_bull():
    feats = compute_macro_features(_series_bull())
    assert feats["macro_regime"] == "macro_bull"
    assert feats["price_vs_50d"] > 0
    assert feats["price_vs_200d"] > 0
    assert feats["spread_50_200"] > 0
    assert feats["slope_50d"] is not None and feats["slope_50d"] > 0


def test_macro_bear():
    feats = compute_macro_features(_series_bear())
    assert feats["macro_regime"] == "macro_bear"
    assert feats["price_vs_50d"] < 0
    assert feats["price_vs_200d"] < 0
    assert feats["spread_50_200"] < 0


def test_macro_neutral_when_mixed():
    # Rise then fall: price below 50D but 50D may still be above/below 200D.
    closes = [100.0 + i for i in range(200)] + [300.0 - 2 * i for i in range(60)]
    feats = compute_macro_features(closes)
    assert feats["macro_regime"] in ("macro_neutral", "macro_bear")


def test_macro_unavailable_insufficient_history():
    feats = compute_macro_features([100.0 + i for i in range(60)])  # < 200 candles
    assert feats["macro_regime"] == "unavailable"
    assert feats["macro_error"] == "insufficient_history_for_200d"


def test_macro_unavailable_no_candles():
    feats = compute_macro_features([])
    assert feats["macro_regime"] == "unavailable"
    assert feats["macro_error"] == "no_candles"
