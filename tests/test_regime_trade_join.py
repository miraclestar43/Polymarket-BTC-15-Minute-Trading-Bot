"""
Phase 1 — actual-trade / regime join tests for analyze_trades_by_regime.

Offline, deterministic. Proves the nearest-snapshot join and LONG/SHORT
bucketing math using realized simulated_pnl only (no shadow resolution).
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

import analyze_trades_by_regime as atr


def test_to_ts_parses_iso():
    ts = atr._to_ts("2026-06-12T01:58:02.108065+00:00")
    assert ts is not None and ts > 0
    assert atr._to_ts("not-a-date") is None


def test_find_nearest_snapshot_within_gap():
    snaps = [
        {"ts": 1000.0, "macro_regime": "macro_bull", "intraday_regime": "bullish",
         "volatility_regime": "low", "combined_regime": "macro_bull|bullish"},
        {"ts": 2000.0, "macro_regime": "macro_bear", "intraday_regime": "bearish",
         "volatility_regime": "high", "combined_regime": "macro_bear|bearish"},
    ]
    out = atr.find_nearest_snapshot(1990.0, snaps)
    assert out["macro_regime"] == "macro_bear"
    assert out["join_gap_seconds"] == 10.0


def test_find_nearest_snapshot_outside_gap_is_unavailable():
    snaps = [{"ts": 1000.0, "macro_regime": "macro_bull", "intraday_regime": "bullish",
              "volatility_regime": "low", "combined_regime": "macro_bull|bullish"}]
    out = atr.find_nearest_snapshot(1000.0 + 10 * 3600, snaps, max_gap=1800)
    assert out["macro_regime"] == "unavailable"
    assert out["join_gap_seconds"] is None


def test_find_nearest_snapshot_no_snapshots():
    out = atr.find_nearest_snapshot(123.0, [])
    assert out["combined_regime"] == "unavailable|unavailable"


def test_join_and_report_bucketing():
    snaps = [
        {"ts": atr._to_ts("2026-06-12T01:00:00+00:00"),
         "macro_regime": "macro_bull", "intraday_regime": "bullish",
         "volatility_regime": "normal", "combined_regime": "macro_bull|bullish"},
    ]
    orders = [
        {"timestamp": "2026-06-12T01:00:30+00:00", "side": "long",
         "entry_price": 0.5, "simulated_pnl": 1.0, "event_type": "simulated_order"},
        {"timestamp": "2026-06-12T01:01:00+00:00", "side": "long",
         "entry_price": 0.5, "simulated_pnl": -0.5, "event_type": "simulated_order"},
        {"timestamp": "2026-06-12T01:02:00+00:00", "side": "short",
         "entry_price": 0.5, "simulated_pnl": 2.0, "event_type": "simulated_order"},
    ]
    joined = atr.join_trades_to_regime(orders, snaps)
    assert len(joined) == 3
    assert all(t["macro_regime"] == "macro_bull" for t in joined)

    report = atr.report_by_dimension(joined, "macro_regime")
    bull = report["macro_bull"]
    assert bull["long"]["trades"] == 2
    assert bull["long"]["wins"] == 1
    assert abs(bull["long"]["total_pnl"] - 0.5) < 1e-9
    assert bull["short"]["trades"] == 1
    assert bull["short"]["wins"] == 1
    assert abs(bull["short"]["total_pnl"] - 2.0) < 1e-9


def test_join_unavailable_when_no_nearby_snapshot():
    snaps = [
        {"ts": atr._to_ts("2026-01-01T00:00:00+00:00"),
         "macro_regime": "macro_bull", "intraday_regime": "bullish",
         "volatility_regime": "normal", "combined_regime": "macro_bull|bullish"},
    ]
    orders = [
        {"timestamp": "2026-06-12T01:00:30+00:00", "side": "long",
         "entry_price": 0.5, "simulated_pnl": 1.0, "event_type": "simulated_order"},
    ]
    joined = atr.join_trades_to_regime(orders, snaps)
    assert joined[0]["macro_regime"] == "unavailable"
    assert joined[0]["join_gap_seconds"] is None
