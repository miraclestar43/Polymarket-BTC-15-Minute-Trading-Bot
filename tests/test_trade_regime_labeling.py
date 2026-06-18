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


# ---------------------------------------------------------------------------
# Run-window scoping
# ---------------------------------------------------------------------------
def test_parse_when_epoch_and_iso():
    assert atr._parse_when("1000000") == 1_000_000.0
    assert atr._parse_when(None) is None
    assert atr._parse_when("") is None
    assert atr._parse_when("bad") is None
    iso = atr._parse_when("2026-06-16T05:20:45+00:00")
    assert iso is not None and abs(iso - 1781587245.0) < 1.0


def test_filter_orders_window_inclusive_boundaries():
    orders = [
        {"timestamp": "2026-06-16T05:00:00+00:00", "n": 1},
        {"timestamp": "2026-06-16T06:00:00+00:00", "n": 2},
        {"timestamp": "2026-06-16T07:00:00+00:00", "n": 3},
        {"timestamp": "2026-06-16T08:00:00+00:00", "n": 4},
        {"timestamp": "2026-06-16T09:00:00+00:00", "n": 5},
    ]
    since = atr._parse_when("2026-06-16T06:00:00+00:00")
    until = atr._parse_when("2026-06-16T08:00:00+00:00")
    kept = [o["n"] for o in atr.filter_orders_window(orders, since, until)]
    assert kept == [2, 3, 4]


def test_latest_run_window_from_events_file(tmp_path):
    import json
    logs = tmp_path / "logs"
    logs.mkdir()
    base = 1_700_000_000.0
    events = [
        {"ts": base, "t": "r1s", "event_type": "runner_start", "pid": 1},
        {"ts": base + 100, "t": "r1e", "event_type": "runner_stop", "pid": 1},
        {"ts": base + 200, "t": "r2s", "event_type": "runner_start", "pid": 2},
        {"ts": base + 400, "t": "r2e", "event_type": "runner_stop", "pid": 2},
    ]
    with open(logs / "runner_events.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    win = atr.latest_run_window(log_dir=str(logs))
    assert win["start_ts"] == base + 200
    assert win["stop_ts"] == base + 400


def test_latest_run_window_missing_file(tmp_path):
    assert atr.latest_run_window(log_dir=str(tmp_path / "nope")) is None

