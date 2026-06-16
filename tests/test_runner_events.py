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
