"""
Watchdog startup false-positive regression tests — offline, deterministic.

Reproduces the 2026-07-19T05:20:24 heartbeat_stale event: the watchdog fired
~60s after run start because it compared against a PREVIOUS run's stale
heartbeat file before the child wrote its first fresh heartbeat. The fix scopes
the heartbeat staleness check to ``run_start_ts`` (mirroring the journal/shadow
checks) while preserving real-stall detection via a startup grace.
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

_spec = importlib.util.spec_from_file_location(
    "bot_runner_15m", project_root / "15m_bot_runner.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def _make_monitor(tmp_path, monkeypatch, run_start_ts):
    monkeypatch.setattr(runner, "project_root", tmp_path)
    (tmp_path / "logs").mkdir(exist_ok=True)
    m = runner.RunnerWatchdog(child_pid_func=lambda: os.getpid())
    m.set_run_start(run_start_ts)
    restarts = []
    monkeypatch.setattr(m, "_restart_child",
                        lambda pid, reason: restarts.append(reason))
    return m, restarts


def _write_heartbeat(tmp_path, mtime):
    hb = tmp_path / runner.HEARTBEAT_FILE
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text("{}")
    os.utime(hb, (mtime, mtime))
    return hb


def test_get_file_age_ignores_prior_run_file(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "project_root", tmp_path)
    run_start = time.time()
    _write_heartbeat(tmp_path, mtime=run_start - 10_000)  # prior run
    m = runner.RunnerWatchdog(child_pid_func=lambda: os.getpid())
    # Unscoped: sees the old file. Scoped to run_start: ignores it.
    assert m._get_file_age(runner.HEARTBEAT_FILE) is not None
    assert m._get_file_age(runner.HEARTBEAT_FILE, min_mtime=run_start) is None


def test_no_false_restart_from_prior_run_heartbeat(tmp_path, monkeypatch):
    run_start = time.time()
    m, restarts = _make_monitor(tmp_path, monkeypatch, run_start)
    # Stale heartbeat left by a previous run, well beyond the threshold...
    _write_heartbeat(tmp_path, mtime=run_start - (m.stale_threshold + 5_000))
    # ...but the current run only started ~60s ago (within startup grace).
    monkeypatch.setattr(runner.time, "time", lambda: run_start + 63)
    m._check_health()
    assert restarts == []  # no false heartbeat_stale


def test_fresh_heartbeat_that_is_stale_still_restarts(tmp_path, monkeypatch):
    run_start = time.time()
    m, restarts = _make_monitor(tmp_path, monkeypatch, run_start)
    # A fresh heartbeat (written by THIS run) that then went stale.
    _write_heartbeat(tmp_path, mtime=run_start + 10)
    monkeypatch.setattr(runner.time, "time",
                        lambda: run_start + 10 + m.stale_threshold + 30)
    m._check_health()
    assert restarts == ["heartbeat_stale"]


def test_never_fresh_heartbeat_triggers_startup_stall(tmp_path, monkeypatch):
    run_start = time.time()
    m, restarts = _make_monitor(tmp_path, monkeypatch, run_start)
    # No heartbeat from this run at all, and a full stale window has elapsed.
    monkeypatch.setattr(runner.time, "time",
                        lambda: run_start + m.stale_threshold + 30)
    m._check_health()
    assert restarts == ["heartbeat_never_fresh"]
