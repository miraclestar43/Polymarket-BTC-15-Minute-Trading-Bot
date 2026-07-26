"""
ExecutionEngine startup mode-label — regression test.

Found during the 7-day DRY-RUN closeout audit: every child logged
``Initialized Execution Engine [LIVE]`` even though ``self.dry_run`` (the
flag that actually gates order behaviour, checked elsewhere in this class)
was correctly ``True``. Root cause: the log line read the raw ``dry_run``
constructor argument, which is ``None`` for the process-wide singleton
(``get_execution_engine()`` calls ``ExecutionEngine(dry_run=None)``), and
``None`` is falsy regardless of the effective config. Purely a misleading
log label — no trading-gate behaviour was affected — but worth pinning down
so it can't mislead an operator scanning logs for a live-mode indicator.
"""
import sys
from pathlib import Path

from loguru import logger

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from execution.execution_engine import ExecutionEngine  # noqa: E402


def _capture_logs():
    """Attach a synchronous in-memory loguru sink; returns (sink_id, lines)."""
    lines = []
    sink_id = logger.add(lines.append, level="INFO")
    return sink_id, lines


def test_mode_label_reflects_effective_dry_run_when_arg_omitted(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ACK", "")
    sink_id, lines = _capture_logs()
    try:
        engine = ExecutionEngine(dry_run=None)  # singleton-style -> loads from config
    finally:
        logger.remove(sink_id)
    assert engine.dry_run is True  # config default
    startup_lines = [str(m) for m in lines if "Initialized Execution Engine" in str(m)]
    assert startup_lines, "startup log line not found"
    assert "[DRY RUN]" in startup_lines[-1]
    assert "[LIVE]" not in startup_lines[-1]


def test_mode_label_reflects_explicit_live_flag():
    sink_id, lines = _capture_logs()
    try:
        engine = ExecutionEngine(dry_run=False)
    finally:
        logger.remove(sink_id)
    assert engine.dry_run is False
    startup_lines = [str(m) for m in lines if "Initialized Execution Engine" in str(m)]
    assert startup_lines and "[LIVE]" in startup_lines[-1]


def test_mode_label_reflects_explicit_dry_run_flag():
    sink_id, lines = _capture_logs()
    try:
        engine = ExecutionEngine(dry_run=True)
    finally:
        logger.remove(sink_id)
    assert engine.dry_run is True
    startup_lines = [str(m) for m in lines if "Initialized Execution Engine" in str(m)]
    assert startup_lines and "[DRY RUN]" in startup_lines[-1]
