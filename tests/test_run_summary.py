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
