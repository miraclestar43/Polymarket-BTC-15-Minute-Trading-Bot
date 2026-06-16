"""
Actual-trade regime labeling tests.

Confirms the analyzer flags small samples as PRELIMINARY and continues to use
journal simulated_pnl with nearest-snapshot joins (no shadow resolution).
"""
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

import analyze_trades_by_regime as atr


def _snap(t_iso, ts):
    return {
        "ts": ts, "t": t_iso,
        "macro_regime": "macro_bull", "intraday_regime": "bullish",
        "volatility_regime": "normal", "combined_regime": "macro_bull|bullish",
    }


def test_small_sample_banner_printed():
    snaps = [_snap("2026-06-12T01:00:00+00:00", atr._to_ts("2026-06-12T01:00:00+00:00"))]
    orders = [
        {"timestamp": "2026-06-12T01:00:30+00:00", "side": "long",
         "entry_price": 0.5, "simulated_pnl": 1.0, "event_type": "simulated_order"}
        for _ in range(5)
    ]

    joined = atr.join_trades_to_regime(orders, snaps)
    buf = io.StringIO()
    with redirect_stdout(buf):
        atr._print_dimension("BY MACRO REGIME", atr.report_by_dimension(joined, "macro_regime"))
    out = buf.getvalue()
    assert "PRELIMINARY" in out


def test_no_banner_for_large_bucket():
    snaps = [_snap("2026-06-12T01:00:00+00:00", atr._to_ts("2026-06-12T01:00:00+00:00"))]
    orders = [
        {"timestamp": "2026-06-12T01:00:30+00:00", "side": "long",
         "entry_price": 0.5, "simulated_pnl": 1.0, "event_type": "simulated_order"}
        for _ in range(atr._SMALL_SAMPLE_THRESHOLD + 5)
    ]
    joined = atr.join_trades_to_regime(orders, snaps)
    buf = io.StringIO()
    with redirect_stdout(buf):
        atr._print_dimension("BY MACRO REGIME", atr.report_by_dimension(joined, "macro_regime"))
    out = buf.getvalue()
    assert "PRELIMINARY" not in out


def test_join_uses_pnl_not_shadow():
    snaps = [_snap("2026-06-12T01:00:00+00:00", atr._to_ts("2026-06-12T01:00:00+00:00"))]
    orders = [
        {"timestamp": "2026-06-12T01:00:30+00:00", "side": "short",
         "entry_price": 0.5, "simulated_pnl": 2.5, "event_type": "simulated_order",
         # A shadow/resolution field must be ignored entirely.
         "resolved_outcome": "DOWN", "resolution_status": "resolved"},
    ]
    joined = atr.join_trades_to_regime(orders, snaps)
    rep = atr.report_by_dimension(joined, "macro_regime")
    assert rep["macro_bull"]["short"]["total_pnl"] == 2.5
    assert rep["macro_bull"]["short"]["wins"] == 1
