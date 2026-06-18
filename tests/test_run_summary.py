"""
Run summary aggregation tests.

Offline, deterministic. Validates runtime aggregation across restarts, no
double-counting of watchdog vs planned restarts, and the small-sample flag.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

import run_summary as rs


def _events_across_restarts():
    # Simulates ~70h of wall clock with three children, summing to ~70h uptime.
    base = 1_000_000.0
    hour = 3600.0
    return [
        {"ts": base, "t": "t0", "event_type": "runner_start"},
        {"ts": base, "t": "t0", "event_type": "child_start", "child_pid": 1, "restart_index": 1},
        # child 1 runs 24h then planned restart
        {"ts": base + 24 * hour, "t": "t1", "event_type": "child_exit", "child_pid": 1,
         "exit_code": -15, "child_uptime_s": 24 * hour, "watchdog_triggered": False,
         "classification": "planned_restart"},
        {"ts": base + 24 * hour, "t": "t1", "event_type": "child_start", "child_pid": 2, "restart_index": 2},
        # child 2 runs 24h then watchdog restart (must NOT be planned)
        {"ts": base + 48 * hour, "t": "t2", "event_type": "watchdog_restart", "child_pid": 2,
         "reason": "heartbeat_stale"},
        {"ts": base + 48 * hour, "t": "t2", "event_type": "child_exit", "child_pid": 2,
         "exit_code": -15, "child_uptime_s": 24 * hour, "watchdog_triggered": True,
         "classification": "watchdog_restart"},
        {"ts": base + 48 * hour, "t": "t2", "event_type": "child_start", "child_pid": 3, "restart_index": 3},
        # child 3 runs 22h then external sigterm
        {"ts": base + 70 * hour, "t": "t3", "event_type": "child_exit", "child_pid": 3,
         "exit_code": -15, "child_uptime_s": 22 * hour, "watchdog_triggered": False,
         "classification": "external_sigterm"},
    ]


def test_wall_clock_and_cumulative_uptime():
    out = rs.summarize_runner_events(_events_across_restarts())
    assert out["wall_clock_span_s"] == 70 * 3600
    # 24 + 24 + 22 = 70h
    assert out["cumulative_child_uptime_s"] == 70 * 3600
    assert out["child_starts"] == 3
    assert out["child_exits"] == 3


def test_no_double_count_watchdog_vs_planned():
    out = rs.summarize_runner_events(_events_across_restarts())
    causes = out["restart_by_cause"]
    assert causes.get("planned_restart") == 1
    assert causes.get("watchdog_restart") == 1
    assert causes.get("external_sigterm") == 1
    # The explicit watchdog event is counted separately, exactly once.
    assert out["watchdog_restart_events"] == 1
    # Planned must not absorb the watchdog restart.
    assert causes.get("planned_restart") != 2


def test_open_child_estimated_uptime():
    base = 1_000_000.0
    events = [
        {"ts": base, "t": "t0", "event_type": "runner_start"},
        {"ts": base, "t": "t0", "event_type": "child_start", "child_pid": 1},
        {"ts": base + 3600, "t": "t1", "event_type": "child_exit", "child_pid": 1,
         "child_uptime_s": 3600, "classification": "planned_restart"},
        {"ts": base + 3600, "t": "t1", "event_type": "child_start", "child_pid": 2},  # no exit yet
        {"ts": base + 5400, "t": "t2", "event_type": "watchdog_restart", "child_pid": 2},
    ]
    out = rs.summarize_runner_events(events)
    assert out["open_child"] is True
    # 3600 (child 1) + (5400-3600)=1800 estimated for the still-open child 2
    assert out["cumulative_child_uptime_s"] == 5400


def test_small_sample_flag():
    orders = [{"side": "long", "simulated_pnl": 1.0} for _ in range(12)]
    t = rs.summarize_trades(orders)
    assert t["trades"] == 12
    assert t["preliminary"] is True


def test_large_sample_not_preliminary():
    orders = [{"side": "short", "simulated_pnl": -0.1} for _ in range(40)]
    t = rs.summarize_trades(orders)
    assert t["preliminary"] is False


def test_build_summary_warns_on_watchdog(tmp_path, monkeypatch):
    # Write a minimal runner_events file and confirm warnings surface.
    logs = tmp_path / "logs"
    logs.mkdir()
    import json
    with open(logs / "runner_events.jsonl", "w") as f:
        for e in _events_across_restarts():
            f.write(json.dumps(e) + "\n")
    summary = rs.build_summary(log_dir=str(logs))
    joined = " ".join(summary["warnings"]).lower()
    assert "watchdog" in joined
    assert "external sigterm" in joined
    assert "preliminary" in joined  # 0 trades < threshold


# ---------------------------------------------------------------------------
# Open-interval / abrupt-stop handling (smoke-test reporting fix)
# ---------------------------------------------------------------------------
def _open_run_events():
    """runner_start + child_start only — the abrupt-kill smoke-test scenario."""
    base = 1_000_000.0
    return [
        {"ts": base, "t": "t0", "event_type": "runner_start", "pid": 999_999},
        {"ts": base + 1, "t": "t0", "event_type": "child_start", "child_pid": 1, "restart_index": 1},
    ]


def test_open_interval_nonzero_with_close_ts():
    base = 1_000_000.0
    # Close the open interval 45 minutes after start (e.g. latest regime ts).
    out = rs.summarize_runner_events(_open_run_events(), close_ts=base + 45 * 60, active=False)
    assert out["open_child"] is True
    assert out["open_runner"] is True
    assert out["wall_clock_span_s"] == 45 * 60          # NOT zero
    # child_start is at base+1, closed at base+2700 -> 2699s
    assert out["cumulative_child_uptime_s"] == 45 * 60 - 1
    assert out["run_status"] == "abruptly_stopped"


def test_open_interval_active_status():
    out = rs.summarize_runner_events(_open_run_events(), close_ts=1_000_000.0 + 600, active=True)
    assert out["run_status"] == "active"
    assert out["wall_clock_span_s"] > 0


def test_open_interval_unknown_status():
    out = rs.summarize_runner_events(_open_run_events(), close_ts=1_000_000.0 + 600, active=None)
    assert out["run_status"] == "active_or_abruptly_stopped"


def test_build_summary_abrupt_stop_not_zero(tmp_path):
    # Reproduce the smoke test: runner_start + child_start, no child_exit / stop,
    # but regime snapshots advance the latest known timestamp by ~45 min.
    import json
    logs = tmp_path / "logs"
    logs.mkdir()
    base = 1_000_000.0
    with open(logs / "runner_events.jsonl", "w") as f:
        for e in _open_run_events():
            f.write(json.dumps(e) + "\n")
    with open(logs / "btc_regime_history.jsonl", "w") as f:
        for i in range(10):
            f.write(json.dumps({"ts": base + i * 300, "combined_regime": "macro_bear|neutral"}) + "\n")
    # pid 999999 is not alive -> abruptly_stopped, closed to latest log ts.
    summary = rs.build_summary(log_dir=str(logs))
    r = summary["runner"]
    assert r["run_status"] == "abruptly_stopped"
    assert r["wall_clock_span_s"] > 0                    # NOT 0h00m00s
    assert r["cumulative_child_uptime_s"] > 0
    joined = " ".join(summary["warnings"]).lower()
    assert "runner_stop not recorded" in joined


def test_completed_run_status():
    base = 1_000_000.0
    events = [
        {"ts": base, "t": "t0", "event_type": "runner_start", "pid": 1},
        {"ts": base, "t": "t0", "event_type": "child_start", "child_pid": 1},
        {"ts": base + 3600, "t": "t1", "event_type": "child_exit", "child_pid": 1,
         "child_uptime_s": 3600, "classification": "runner_shutdown"},
        {"ts": base + 3600, "t": "t1", "event_type": "runner_stop", "pid": 1},
    ]
    out = rs.summarize_runner_events(events, close_ts=base + 9999, active=False)
    assert out["run_status"] == "completed"
    assert out["open_runner"] is False
    # completed run ignores close_ts for the span
    assert out["wall_clock_span_s"] == 3600


# ---------------------------------------------------------------------------
# Run-window scoping
# ---------------------------------------------------------------------------
def test_parse_when_epoch_and_iso():
    assert rs.parse_when("1000000") == 1_000_000.0
    assert rs.parse_when("1000000.5") == 1_000_000.5
    assert rs.parse_when(None) is None
    assert rs.parse_when("") is None
    assert rs.parse_when("not-a-date") is None
    iso = rs.parse_when("2026-06-16T05:20:45+00:00")
    assert iso is not None and abs(iso - 1781587245.0) < 1.0


def test_latest_run_window_picks_most_recent_start():
    base = 1_000_000.0
    events = [
        {"ts": base, "t": "a", "event_type": "runner_start", "pid": 1},
        {"ts": base + 100, "t": "b", "event_type": "runner_stop", "pid": 1},
        {"ts": base + 200, "t": "c", "event_type": "runner_start", "pid": 2},
        {"ts": base + 400, "t": "d", "event_type": "runner_stop", "pid": 2},
    ]
    win = rs.latest_run_window(events)
    assert win["start_ts"] == base + 200
    assert win["stop_ts"] == base + 400


def test_latest_run_window_open_run():
    base = 1_000_000.0
    events = [
        {"ts": base, "t": "a", "event_type": "runner_start", "pid": 1},
        {"ts": base + 100, "t": "b", "event_type": "runner_stop", "pid": 1},
        {"ts": base + 200, "t": "c", "event_type": "runner_start", "pid": 2},
    ]
    win = rs.latest_run_window(events)
    assert win["start_ts"] == base + 200
    assert win["stop_ts"] is None


def test_latest_run_window_no_starts():
    assert rs.latest_run_window([]) is None


def test_filter_orders_window_boundaries():
    orders = [
        {"timestamp": "2026-06-16T05:00:00+00:00", "n": 1},   # before
        {"timestamp": "2026-06-16T06:00:00+00:00", "n": 2},   # at since
        {"timestamp": "2026-06-16T07:00:00+00:00", "n": 3},   # inside
        {"timestamp": "2026-06-16T08:00:00+00:00", "n": 4},   # at until
        {"timestamp": "2026-06-16T09:00:00+00:00", "n": 5},   # after
    ]
    since = rs.parse_when("2026-06-16T06:00:00+00:00")
    until = rs.parse_when("2026-06-16T08:00:00+00:00")
    kept = [o["n"] for o in rs.filter_orders_window(orders, since, until)]
    assert kept == [2, 3, 4]  # inclusive boundaries


def test_filter_events_window_boundaries():
    base = 1_000_000.0
    events = [{"ts": base + i * 100, "n": i} for i in range(5)]
    kept = [e["n"] for e in rs.filter_events_window(events, base + 100, base + 300)]
    assert kept == [1, 2, 3]


def test_build_summary_scoped_window(tmp_path):
    import json
    logs = tmp_path / "logs"
    logs.mkdir()
    base = 1_700_000_000.0
    # Two runs in the events file; only the second should be reported.
    events = [
        {"ts": base, "t": "r1s", "event_type": "runner_start", "pid": 1},
        {"ts": base, "t": "r1s", "event_type": "child_start", "child_pid": 1},
        {"ts": base + 3600, "t": "r1e", "event_type": "child_exit", "child_pid": 1,
         "child_uptime_s": 3600, "classification": "clean_exit"},
        {"ts": base + 3600, "t": "r1e", "event_type": "runner_stop", "pid": 1},
        {"ts": base + 7200, "t": "r2s", "event_type": "runner_start", "pid": 2},
        {"ts": base + 7200, "t": "r2s", "event_type": "child_start", "child_pid": 2},
        {"ts": base + 10800, "t": "r2e", "event_type": "child_exit", "child_pid": 2,
         "child_uptime_s": 3600, "classification": "runner_shutdown"},
        {"ts": base + 10800, "t": "r2e", "event_type": "runner_stop", "pid": 2},
    ]
    with open(logs / "runner_events.jsonl", "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    def _iso(ts):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()

    with open(logs / "trade_journal_2026.jsonl", "w") as f:
        # one trade in run 1, one trade in run 2
        f.write(json.dumps({"event_type": "simulated_order", "side": "long",
                            "simulated_pnl": -1.0, "timestamp": _iso(base + 100)}) + "\n")
        f.write(json.dumps({"event_type": "simulated_order", "side": "short",
                            "simulated_pnl": 2.0, "timestamp": _iso(base + 8000)}) + "\n")

    win = rs.latest_run_window(events)
    summary = rs.build_summary(
        log_dir=str(logs), since=win["start_ts"], until=win["stop_ts"]
    )
    assert summary["window"]["scoped"] is True
    r = summary["runner"]
    assert r["run_status"] == "completed"
    assert r["wall_clock_span_s"] == 3600        # only run 2's span
    assert r["child_starts"] == 1
    # only the run-2 trade survives the window filter
    assert summary["trades"]["trades"] == 1


