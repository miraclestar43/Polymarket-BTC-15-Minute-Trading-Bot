"""
Watchdog diagnostics tests — READ-ONLY analysis, deterministic, offline.

Verifies:
  * loop-wide stall classification (heartbeat + journal + shadow all stale)
  * heartbeat-only vs loop-wide distinction
  * per-restart child_start/child_exit correlation
  * root cause is reported as NOT proven (honest) when logs lack per-call timing
  * window filtering
"""
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

import watchdog_diagnostics as wd


def _write(log_dir, events):
    (log_dir / "runner_events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")


def test_loop_wide_stall(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    events = [
        {"ts": 1000.0, "event_type": "runner_start", "pid": 1},
        {"ts": 1001.0, "event_type": "child_start", "child_pid": 100},
        {"ts": 3700.0, "event_type": "watchdog_restart", "child_pid": 100,
         "reason": "heartbeat_stale", "heartbeat_age_s": 2750.0,
         "journal_age_s": 3000.0, "shadow_age_s": 3000.0},
        {"ts": 3705.0, "event_type": "child_exit", "child_pid": 100,
         "exit_code": 0, "classification": "watchdog_restart"},
    ]
    _write(log_dir, events)
    r = wd.analyze(log_dir=str(log_dir))
    hb = r["heartbeat_stale_breakdown"]
    assert hb["total"] == 1
    assert hb["all_three_files_stale_together"] == 1
    assert hb["heartbeat_only"] == 0
    rr = r["restarts"][0]
    assert rr["all_three_stale_together"] is True
    assert rr["child_uptime_before_restart_s"] == 2699.0
    assert rr["exit_code"] == 0
    assert r["root_cause"]["proven"] is False  # honest: not proven from logs


def test_heartbeat_only_stall(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    events = [
        {"ts": 1001.0, "event_type": "child_start", "child_pid": 100},
        {"ts": 3700.0, "event_type": "watchdog_restart", "child_pid": 100,
         "reason": "heartbeat_stale", "heartbeat_age_s": 2750.0,
         "journal_age_s": 10.0, "shadow_age_s": 10.0},
    ]
    _write(log_dir, events)
    r = wd.analyze(log_dir=str(log_dir))
    hb = r["heartbeat_stale_breakdown"]
    assert hb["all_three_files_stale_together"] == 0
    assert hb["heartbeat_only"] == 1


def test_no_restarts(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write(log_dir, [{"ts": 1.0, "event_type": "child_start", "child_pid": 1}])
    r = wd.analyze(log_dir=str(log_dir))
    assert r["root_cause"]["proven"] is False
    assert "nothing to attribute" in r["root_cause"]["summary"]


def test_window_filter(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    events = [
        {"ts": 500.0, "event_type": "watchdog_restart", "child_pid": 1,
         "reason": "heartbeat_stale", "heartbeat_age_s": 2750.0,
         "journal_age_s": 3000.0, "shadow_age_s": 3000.0},
        {"ts": 5000.0, "event_type": "watchdog_restart", "child_pid": 2,
         "reason": "heartbeat_stale", "heartbeat_age_s": 2750.0,
         "journal_age_s": 3000.0, "shadow_age_s": 3000.0},
    ]
    _write(log_dir, events)
    r = wd.analyze(log_dir=str(log_dir), since=1000.0, until=6000.0)
    assert r["heartbeat_stale_breakdown"]["total"] == 1


def test_thresholds_reported_not_changed(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write(log_dir, [])
    r = wd.analyze(log_dir=str(log_dir))
    assert r["thresholds"]["heartbeat_stale_threshold_s"] == 2700
    assert "NOT changed" in r["thresholds"]["note"]
