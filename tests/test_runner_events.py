"""
Runner lifecycle event classification + logging tests.

Offline, deterministic. Validates classify_exit() and that watchdog restarts
are NOT double-counted as normal/planned restarts.
"""
import importlib.util
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Load the runner module (filename starts with a digit, so import via spec).
_spec = importlib.util.spec_from_file_location(
    "bot_runner_15m", project_root / "15m_bot_runner.py"
)
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)


def test_planned_restart_at_90_min():
    # Signal-terminated at full planned age, not watchdog -> planned_restart.
    c = runner.classify_exit(exit_code=-15, child_uptime_s=90 * 60, watchdog_triggered=False)
    assert c == "planned_restart"


def test_planned_restart_within_tolerance():
    c = runner.classify_exit(exit_code=143, child_uptime_s=86 * 60, watchdog_triggered=False)
    assert c == "planned_restart"


def test_watchdog_restart_not_counted_as_planned():
    # Even at full uptime, an explicit watchdog kill is a watchdog_restart.
    c = runner.classify_exit(exit_code=-15, child_uptime_s=90 * 60, watchdog_triggered=True)
    assert c == "watchdog_restart"


def test_external_sigterm_short_uptime():
    # Signal-terminated early, not by our watchdog -> external.
    c = runner.classify_exit(exit_code=-15, child_uptime_s=60, watchdog_triggered=False)
    assert c == "external_sigterm"


def test_crash_error_nonzero():
    c = runner.classify_exit(exit_code=1, child_uptime_s=120, watchdog_triggered=False)
    assert c == "crash_error"


def test_clean_exit_zero():
    c = runner.classify_exit(exit_code=0, child_uptime_s=120, watchdog_triggered=False)
    assert c == "clean_exit"


def test_log_runner_event_failsafe(monkeypatch, tmp_path):
    # Point the events file at a tmp path and confirm a record is written.
    monkeypatch.setattr(runner, "project_root", tmp_path)
    runner.log_runner_event("child_start", child_pid=1234, restart_index=1)
    out = (tmp_path / "logs" / "runner_events.jsonl").read_text().strip()
    import json
    rec = json.loads(out)
    assert rec["event_type"] == "child_start"
    assert rec["child_pid"] == 1234
    assert "ts" in rec and "t" in rec


def test_log_runner_event_never_raises(monkeypatch):
    # Force an unwritable path; must not raise.
    class Boom:
        def __truediv__(self, other):
            raise RuntimeError("no fs")
    monkeypatch.setattr(runner, "project_root", Boom())
    runner.log_runner_event("child_exit", classification="planned_restart")  # no exception


# ---------------------------------------------------------------------------
# Graceful shutdown machinery — must NOT alter restart classification.
# ---------------------------------------------------------------------------
def test_runner_shutdown_is_base_exception():
    # Inheriting BaseException keeps it from being swallowed by `except Exception`.
    assert issubclass(runner.RunnerShutdown, BaseException)
    assert not issubclass(runner.RunnerShutdown, Exception)


def test_runner_shutdown_carries_signum():
    exc = runner.RunnerShutdown(15)
    assert exc.signum == 15


def test_signal_name_resolves():
    import signal as _signal
    assert runner._signal_name(_signal.SIGTERM) == "SIGTERM"
    assert runner._signal_name(None) is None


def test_shutdown_does_not_change_classification():
    # The shutdown path logs classification="runner_shutdown" directly; the
    # normal classify_exit() logic is unchanged for every signal/uptime case.
    assert runner.classify_exit(-15, 90 * 60, False) == "planned_restart"
    assert runner.classify_exit(-15, 60, False) == "external_sigterm"
    assert runner.classify_exit(-15, 90 * 60, True) == "watchdog_restart"
    assert runner.classify_exit(0, 60, False) == "clean_exit"
    assert runner.classify_exit(1, 60, False) == "crash_error"
    # "runner_shutdown" is never produced by classify_exit.
    for code in (-15, 0, 1, 143):
        for up in (0, 60, 5400):
            assert runner.classify_exit(code, up, False) != "runner_shutdown"


# ---------------------------------------------------------------------------
# Watchdog journal staleness must be scoped to the CURRENT run session.
# Files from previous runs/days must NOT trigger journal_stale restarts.
# ---------------------------------------------------------------------------
import os as _os
import time as _time


def _make_journal(dir_path, name, age_seconds):
    f = dir_path / name
    f.write_text('{"event_type": "simulated_order"}\n')
    mtime = _time.time() - age_seconds
    _os.utime(f, (mtime, mtime))
    return f


def _watchdog_with_root(monkeypatch, tmp_path):
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(runner, "project_root", tmp_path)
    return runner.RunnerWatchdog(child_pid_func=lambda: 4242)


_HB_RUN_ID = "run_test_runner_events"


def _watchdog_with_identity(monkeypatch, tmp_path, child_start_ts, generation=1):
    """A RunnerWatchdog with full run/child identity set, for tests that
    need the (now identity-checked) heartbeat path to actually validate."""
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(runner, "project_root", tmp_path)
    active_child = {"start_ts": child_start_ts, "restart_index": generation}
    return runner.RunnerWatchdog(
        child_pid_func=lambda: 4242, run_id=_HB_RUN_ID, active_child=active_child,
    )


def _write_matching_heartbeat(tmp_path, ts, generation=1):
    from core.diagnostics import run_provenance as rp
    import json
    rp.write_heartbeat(
        repo_root=tmp_path, run_id=_HB_RUN_ID, runner_pid=_os.getpid(),
        child_pid=4242, child_generation=str(generation), child_start_ts=ts,
        event="tick", uptime_s=0, git_head="deadbeef", schema_version=2,
    )
    path = rp.heartbeat_path(tmp_path, _HB_RUN_ID)
    data = json.loads(path.read_text())
    data["timestamp"] = ts
    path.write_text(json.dumps(data))


def test_old_journal_ignored_when_scoped_to_run_start(monkeypatch, tmp_path):
    wd = _watchdog_with_root(monkeypatch, tmp_path)
    logs = tmp_path / "logs"
    # A journal from a previous run, 3 hours old.
    _make_journal(logs, "trade_journal_2026-06-15.jsonl", age_seconds=3 * 3600)
    # The current run started just now.
    wd.set_run_start(_time.time())
    # Scoped check: the old file is excluded -> no current-run journal -> None.
    age = wd._get_newest_file_age(runner.JOURNAL_DIR, "trade_journal_*.jsonl",
                                  min_mtime=wd.run_start_ts)
    assert age is None


def test_current_run_journal_is_seen(monkeypatch, tmp_path):
    wd = _watchdog_with_root(monkeypatch, tmp_path)
    logs = tmp_path / "logs"
    wd.set_run_start(_time.time() - 600)  # run started 10 min ago
    # A journal written 5 min ago (within the current run).
    _make_journal(logs, "trade_journal_2026-06-16.jsonl", age_seconds=300)
    age = wd._get_newest_file_age(runner.JOURNAL_DIR, "trade_journal_*.jsonl",
                                  min_mtime=wd.run_start_ts)
    assert age is not None
    assert 250 < age < 360


def test_check_health_does_not_restart_on_old_journal(monkeypatch, tmp_path):
    base = _time.time()
    wd = _watchdog_with_identity(monkeypatch, tmp_path, child_start_ts=base)
    logs = tmp_path / "logs"
    # Fresh, correctly-identified run-owned heartbeat (healthy), but only an
    # OLD journal from a previous run.
    _write_matching_heartbeat(tmp_path, ts=base + 5)
    _make_journal(logs, "trade_journal_2026-06-15.jsonl", age_seconds=10000)
    wd.set_run_start(base)

    monkeypatch.setattr(runner.os, "kill", lambda *a, **k: None)  # child "alive"
    monkeypatch.setattr(runner.time, "time", lambda: base + 10)
    calls = []
    monkeypatch.setattr(wd, "_restart_child", lambda pid, reason: calls.append(reason))

    wd._check_health()
    assert calls == []  # old journal must NOT trigger a restart


def test_check_health_still_restarts_on_stale_heartbeat(monkeypatch, tmp_path):
    base = _time.time()
    wd = _watchdog_with_identity(monkeypatch, tmp_path, child_start_ts=base)
    wd.set_run_start(base)
    # A FRESH, correctly-identified heartbeat written by THIS run's current
    # child that then goes stale.
    fresh = base + 10
    _write_matching_heartbeat(tmp_path, ts=fresh)
    monkeypatch.setattr(runner.time, "time",
                        lambda: fresh + runner.STALE_THRESHOLD_SECONDS + 600)

    monkeypatch.setattr(runner.os, "kill", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(wd, "_restart_child", lambda pid, reason: calls.append(reason))

    wd._check_health()
    assert calls == ["heartbeat_stale"]  # genuine stall still detected


# ---------------------------------------------------------------------------
# run_id must be present on every new lifecycle event (2026-07-27 isolation
# hardening) -- logs/runner_events.jsonl stays a single shared file, but
# every record must be unambiguously attributable to a run_id so it can be
# scoped correctly even while another run is concurrently appending to the
# same file (exactly what happened with run_1785102520_55073).
# ---------------------------------------------------------------------------
def test_log_runner_event_carries_current_run_id(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "project_root", tmp_path)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", "run_TEST_123")
    for event_type, kwargs in [
        ("runner_start", {"pid": 1}),
        ("child_start", {"child_pid": 2, "restart_index": 1}),
        ("child_exit", {"child_pid": 2, "restart_index": 1, "exit_code": 0}),
        ("watchdog_restart", {"child_pid": 2, "reason": "heartbeat_stale"}),
        ("runner_stop", {"pid": 1, "signal": "SIGTERM"}),
    ]:
        runner.log_runner_event(event_type, **kwargs)

    lines = (tmp_path / "logs" / "runner_events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 5
    import json
    for line in lines:
        rec = json.loads(line)
        assert rec["run_id"] == "run_TEST_123"


def test_log_runner_event_run_id_none_before_run_bot_sets_it(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "project_root", tmp_path)
    monkeypatch.setattr(runner, "_CURRENT_RUN_ID", None)
    runner.log_runner_event("child_start", child_pid=1)
    import json
    rec = json.loads((tmp_path / "logs" / "runner_events.jsonl").read_text().strip())
    assert rec["run_id"] is None  # explicit, not a crash or a missing key
    assert "run_id" in rec


