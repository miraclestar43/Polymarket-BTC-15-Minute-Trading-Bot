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
    wd = _watchdog_with_root(monkeypatch, tmp_path)
    logs = tmp_path / "logs"
    # Fresh heartbeat (healthy), but only an OLD journal from a previous run.
    hb = logs / "bot_heartbeat.json"
    hb.write_text("{}")  # fresh mtime
    _make_journal(logs, "trade_journal_2026-06-15.jsonl", age_seconds=10000)
    wd.set_run_start(_time.time())

    monkeypatch.setattr(runner.os, "kill", lambda *a, **k: None)  # child "alive"
    calls = []
    monkeypatch.setattr(wd, "_restart_child", lambda pid, reason: calls.append(reason))

    wd._check_health()
    assert calls == []  # old journal must NOT trigger a restart


def test_check_health_still_restarts_on_stale_heartbeat(monkeypatch, tmp_path):
    wd = _watchdog_with_root(monkeypatch, tmp_path)
    logs = tmp_path / "logs"
    hb = logs / "bot_heartbeat.json"
    hb.write_text("{}")
    base = _time.time()
    wd.set_run_start(base)
    # A FRESH heartbeat written by THIS run (after run start) that then goes
    # stale. A heartbeat older than run_start is a prior-run artifact and is
    # intentionally ignored (see test_watchdog_startup_falsepositive).
    fresh = base + 10
    _os.utime(hb, (fresh, fresh))
    monkeypatch.setattr(runner.time, "time",
                        lambda: fresh + runner.STALE_THRESHOLD_SECONDS + 600)

    monkeypatch.setattr(runner.os, "kill", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(wd, "_restart_child", lambda pid, reason: calls.append(reason))

    wd._check_health()
    assert calls == ["heartbeat_stale"]  # genuine stall still detected


