"""
Watchdog heartbeat identity regression tests — offline, deterministic.

History: the 2026-07-19T05:20:24 false positive fired ~60s after run start
because the watchdog compared against a PREVIOUS run's stale heartbeat file
before the child wrote its first fresh heartbeat. That was fixed by scoping
staleness to run_start_ts.

2026-07-27: a stronger, structural version of the same class of bug was
found -- when two runner sessions existed at once (Week-1 and an aborted
Week-2 pilot), BOTH shared the same legacy logs/bot_heartbeat.json, so
either child could refresh the heartbeat the OTHER runner's watchdog was
reading, masking a real stall. The heartbeat is now run-owned
(logs/runs/<run_id>/heartbeat.json, a unique path per run_id) and carries
explicit identity (run_id, runner_pid, child_pid, child_generation), and
the watchdog validates ALL of these plus timestamp-vs-child-start before
ever treating a heartbeat as evidence of liveness. These tests prove a
heartbeat from another run, or from an old child of the SAME run, is never
accepted -- while a real stall (or startup delay) is still caught exactly
as before, with the same threshold.
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.diagnostics import run_provenance as rp  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "bot_runner_15m", project_root / "15m_bot_runner.py")
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


RUN_ID = "run_1700000000_1111"
RUNNER_PID = os.getpid()  # so the watchdog's os.getpid() check passes
# _check_health() liveness-checks the child with os.kill(pid, 0) before ever
# reaching the heartbeat logic, so CHILD_PID must be a real, existing PID --
# use this test process's own PID, exactly like the process it stands in for.
CHILD_PID = os.getpid()


def _make_monitor(tmp_path, monkeypatch, run_start_ts, child_start_ts, generation=1):
    monkeypatch.setattr(runner, "project_root", tmp_path)
    (tmp_path / "logs").mkdir(exist_ok=True)
    active_child = {"start_ts": child_start_ts, "restart_index": generation}
    m = runner.RunnerWatchdog(
        child_pid_func=lambda: CHILD_PID, run_id=RUN_ID, active_child=active_child,
    )
    m.set_run_start(run_start_ts)
    restarts = []
    monkeypatch.setattr(m, "_restart_child",
                        lambda pid, reason: restarts.append(reason))
    return m, restarts


def _write_hb(tmp_path, ts, run_id=RUN_ID, runner_pid=RUNNER_PID,
              child_pid=CHILD_PID, generation="1"):
    rp.write_heartbeat(
        repo_root=tmp_path, run_id=run_id, runner_pid=runner_pid,
        child_pid=child_pid, child_generation=generation, child_start_ts=ts,
        event="tick", uptime_s=0, git_head="deadbeef", schema_version=2,
    )
    # write_heartbeat always stamps "timestamp" with time.time() at call
    # time; overwrite it directly so tests can control heartbeat age.
    path = rp.heartbeat_path(tmp_path, run_id)
    import json
    data = json.loads(path.read_text())
    data["timestamp"] = ts
    path.write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# No valid heartbeat yet (startup grace vs genuine never-fresh)
# ---------------------------------------------------------------------------
def test_no_false_restart_during_startup_grace(tmp_path, monkeypatch):
    run_start = time.time()
    m, restarts = _make_monitor(tmp_path, monkeypatch, run_start, child_start_ts=run_start)
    monkeypatch.setattr(runner.time, "time", lambda: run_start + 63)  # well within grace
    m._check_health()
    assert restarts == []


def test_never_fresh_heartbeat_triggers_startup_stall(tmp_path, monkeypatch):
    run_start = time.time()
    m, restarts = _make_monitor(tmp_path, monkeypatch, run_start, child_start_ts=run_start)
    monkeypatch.setattr(runner.time, "time",
                        lambda: run_start + m.stale_threshold + 30)
    m._check_health()
    assert restarts == ["heartbeat_never_fresh"]


# ---------------------------------------------------------------------------
# A valid, correctly-identified heartbeat is accepted
# ---------------------------------------------------------------------------
def test_fresh_matching_heartbeat_is_accepted_no_restart(tmp_path, monkeypatch):
    run_start = time.time()
    m, restarts = _make_monitor(tmp_path, monkeypatch, run_start, child_start_ts=run_start)
    _write_hb(tmp_path, ts=run_start + 5, generation="1")
    monkeypatch.setattr(runner.time, "time", lambda: run_start + 10)
    m._check_health()
    assert restarts == []


def test_fresh_heartbeat_that_goes_stale_still_restarts(tmp_path, monkeypatch):
    run_start = time.time()
    m, restarts = _make_monitor(tmp_path, monkeypatch, run_start, child_start_ts=run_start)
    _write_hb(tmp_path, ts=run_start + 10, generation="1")
    monkeypatch.setattr(runner.time, "time",
                        lambda: run_start + 10 + m.stale_threshold + 30)
    m._check_health()
    assert restarts == ["heartbeat_stale"]


# ---------------------------------------------------------------------------
# A heartbeat from ANOTHER RUN must never refresh liveness
# ---------------------------------------------------------------------------
def test_heartbeat_from_another_run_id_is_rejected(tmp_path, monkeypatch):
    run_start = time.time()
    m, restarts = _make_monitor(tmp_path, monkeypatch, run_start, child_start_ts=run_start)
    # A different run wrote a fresh-looking heartbeat under a DIFFERENT
    # run_id path -- write_heartbeat writes to logs/runs/<that run_id>/...,
    # so simulate the failure mode directly by writing content with a
    # mismatched run_id field into THIS run's own path (defense-in-depth:
    # the field must be validated even if the path were ever wrong).
    _write_hb(tmp_path, ts=run_start + 5, run_id="run_OTHER_9999", generation="1")
    # Force-write it at THIS run's path so the path lookup finds it.
    other_path = rp.heartbeat_path(tmp_path, "run_OTHER_9999")
    this_path = rp.heartbeat_path(tmp_path, RUN_ID)
    this_path.parent.mkdir(parents=True, exist_ok=True)
    this_path.write_text(other_path.read_text())
    monkeypatch.setattr(runner.time, "time", lambda: run_start + 10)
    m._check_health()
    assert restarts == []  # within startup grace -- treated as "no valid heartbeat", not a restart
    hb, reason = m._valid_heartbeat(CHILD_PID)
    assert hb is None and reason == "heartbeat_wrong_run"


def test_heartbeat_from_another_runner_pid_is_rejected(tmp_path, monkeypatch):
    run_start = time.time()
    m, restarts = _make_monitor(tmp_path, monkeypatch, run_start, child_start_ts=run_start)
    _write_hb(tmp_path, ts=run_start + 5, runner_pid=RUNNER_PID + 999, generation="1")
    hb, reason = m._valid_heartbeat(CHILD_PID)
    assert hb is None and reason == "heartbeat_wrong_run"


# ---------------------------------------------------------------------------
# A heartbeat from an OLD CHILD of the SAME run must never refresh liveness
# ---------------------------------------------------------------------------
def test_heartbeat_from_previous_child_pid_is_rejected(tmp_path, monkeypatch):
    run_start = time.time()
    old_child_start = run_start
    new_child_start = run_start + 5500  # after a ~90min restart
    m, restarts = _make_monitor(
        tmp_path, monkeypatch, run_start, child_start_ts=new_child_start, generation=2,
    )
    # Old child (generation 1, different pid) wrote this heartbeat; the new
    # child (generation 2) hasn't written its first one yet.
    _write_hb(tmp_path, ts=new_child_start + 2, child_pid=CHILD_PID - 1, generation="1")
    monkeypatch.setattr(runner.time, "time", lambda: new_child_start + 10)
    hb, reason = m._valid_heartbeat(CHILD_PID)
    assert hb is None and reason == "heartbeat_wrong_child"
    m._check_health()
    assert restarts == []  # still within the new child's own startup grace


def test_new_child_cannot_reuse_old_child_heartbeat_to_avoid_never_fresh(tmp_path, monkeypatch):
    run_start = time.time()
    new_child_start = run_start + 5500
    m, restarts = _make_monitor(
        tmp_path, monkeypatch, run_start, child_start_ts=new_child_start, generation=2,
    )
    # Old child's heartbeat is fresh by wall-clock but belongs to generation 1.
    _write_hb(tmp_path, ts=new_child_start + 100, child_pid=CHILD_PID - 1, generation="1")
    # A full stale window has elapsed since the NEW child started, and it
    # never produced its own valid heartbeat -- must restart, not be fooled
    # by the old child's fresh-looking one.
    monkeypatch.setattr(runner.time, "time",
                        lambda: new_child_start + m.stale_threshold + 30)
    m._check_health()
    assert restarts == ["heartbeat_never_fresh"]


def test_heartbeat_older_than_current_child_start_is_rejected(tmp_path, monkeypatch):
    run_start = time.time()
    new_child_start = run_start + 5500
    m, restarts = _make_monitor(
        tmp_path, monkeypatch, run_start, child_start_ts=new_child_start, generation=2,
    )
    # Same pid/generation numbers reused is implausible, but the timestamp
    # guard is independent defense-in-depth: a heartbeat timestamped BEFORE
    # this child started can never count, regardless of matching identity.
    _write_hb(tmp_path, ts=new_child_start - 50, child_pid=CHILD_PID, generation="2")
    hb, reason = m._valid_heartbeat(CHILD_PID)
    assert hb is None and reason == "heartbeat_wrong_child"


# ---------------------------------------------------------------------------
# Atomic write + bounded/parseable content
# ---------------------------------------------------------------------------
def test_heartbeat_write_is_atomic_and_readback_matches(tmp_path):
    path = rp.write_heartbeat(
        repo_root=tmp_path, run_id=RUN_ID, runner_pid=RUNNER_PID, child_pid=CHILD_PID,
        child_generation="1", child_start_ts=1700000000.0, event="quote_tick",
        uptime_s=42, git_head="cafef00d", schema_version=2,
    )
    assert path == rp.heartbeat_path(tmp_path, RUN_ID)
    hb = rp.read_heartbeat(tmp_path, RUN_ID)
    assert hb["run_id"] == RUN_ID
    assert hb["runner_pid"] == RUNNER_PID
    assert hb["child_pid"] == CHILD_PID
    assert hb["child_generation"] == "1"
    assert hb["event"] == "quote_tick"
    assert hb["git_head"] == "cafef00d"
    assert hb["schema_version"] == 2
    # bounded size: a small flat dict, comfortably under 1KB
    assert path.stat().st_size < 1024
    assert list(path.parent.glob(".heartbeat.json.*.tmp")) == []


def test_heartbeat_path_is_run_scoped(tmp_path):
    p1 = rp.heartbeat_path(tmp_path, "run_A")
    p2 = rp.heartbeat_path(tmp_path, "run_B")
    assert p1 != p2
    assert "run_A" in str(p1) and "run_B" in str(p2)


def test_read_heartbeat_missing_file_returns_none(tmp_path):
    assert rp.read_heartbeat(tmp_path, "run_does_not_exist") is None
