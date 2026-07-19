"""Runtime diagnostics for the trading bot (Part C — stall investigation).

Purpose: capture enough evidence to identify the ROOT CAUSE of the recurring
"heartbeat_stale" event-loop stalls that force watchdog restarts, WITHOUT
changing any trading logic.

What this provides:
  * A bounded, thread-safe ring buffer of recent blocking-operation timings
    (`record_op`, `timed`, `atimed`) — so post-mortem we can see which network
    call ran long right before a stall.
  * An event-loop lag monitor (`EventLoopLagMonitor`) that records when an
    asyncio loop fails to wake on schedule (the direct symptom of a stall).
  * On-demand full-process dumps triggered by a signal (default SIGUSR1 for a
    native all-thread C-level traceback via faulthandler, SIGUSR2 for a rich
    JSON snapshot with per-thread Python stacks, asyncio task states, recent
    op timings and CPU/memory). The native handler works even if the Python
    interpreter is wedged holding the GIL.
  * Secret redaction so dumps never leak keys/tokens.

Everything is best-effort and fail-safe: diagnostics must never crash or slow
the bot, and never alter trading behaviour.
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OP_BUFFER_SIZE = 250          # max recent op records retained
LAG_BUFFER_SIZE = 100         # max recent loop-lag records retained
DEFAULT_DUMP_DIR = "logs/diagnostics"
# Generous default network timeout for instrumented awaits. Chosen well above
# normal latency so healthy operation is unaffected, but far below the 45-min
# watchdog threshold so a wedged socket surfaces quickly instead of stalling
# the whole event loop.
NET_TIMEOUT_S = 20.0

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"

# Keys / substrings whose values must be redacted from any dump.
_SECRET_KEY_RE = re.compile(
    r"(private[_-]?key|api[_-]?key|api[_-]?secret|secret|passphrase|password|"
    r"token|authorization|auth|bearer|cookie|session)",
    re.IGNORECASE,
)
# Free-text patterns like `api_key=abcd...` or `"secret": "abcd"`.
_SECRET_TEXT_RE = re.compile(
    r"((?:private[_-]?key|api[_-]?key|api[_-]?secret|secret|passphrase|password|"
    r"token|authorization|auth|bearer)\s*[=:]\s*[\"']?)([^\s\"',}]+)",
    re.IGNORECASE,
)
_REDACTED = "***REDACTED***"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def redact_text(text: str) -> str:
    """Mask secret-looking `key=value` / `key: value` pairs in free text."""
    if not text:
        return text
    return _SECRET_TEXT_RE.sub(lambda m: m.group(1) + _REDACTED, text)


def redact(obj: Any, _depth: int = 0) -> Any:
    """Recursively redact secret values from a JSON-serialisable structure."""
    if _depth > 6:
        return "<max-depth>"
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k):
                out[k] = _REDACTED
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, _depth + 1) for v in obj][:200]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


# ---------------------------------------------------------------------------
# Op timing ring buffer
# ---------------------------------------------------------------------------

class _DiagState:
    """Process-global diagnostics state (single instance below)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ops: Deque[Dict[str, Any]] = deque(maxlen=OP_BUFFER_SIZE)
        self.lags: Deque[Dict[str, Any]] = deque(maxlen=LAG_BUFFER_SIZE)
        self._loops: "list[Any]" = []  # registered asyncio loops (best-effort)
        self.dump_dir = Path(DEFAULT_DUMP_DIR)
        self._fault_file = None  # kept open for faulthandler.register

    def record_op(self, record: Dict[str, Any]) -> None:
        with self._lock:
            self.ops.append(record)

    def record_lag(self, record: Dict[str, Any]) -> None:
        with self._lock:
            self.lags.append(record)

    def snapshot_ops(self, limit: int = OP_BUFFER_SIZE) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.ops)[-limit:]

    def snapshot_lags(self, limit: int = LAG_BUFFER_SIZE) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.lags)[-limit:]

    def register_loop(self, loop: Any) -> None:
        with self._lock:
            if loop not in self._loops:
                self._loops.append(loop)

    def loops(self) -> List[Any]:
        with self._lock:
            return list(self._loops)


_STATE = _DiagState()


def record_op(name: str, duration_s: float, status: str = STATUS_OK,
              error: Optional[str] = None, **meta: Any) -> None:
    """Append a completed-operation timing record to the ring buffer."""
    rec: Dict[str, Any] = {
        "t": datetime.now(timezone.utc).isoformat(),
        "ts": round(time.time(), 3),
        "op": name,
        "duration_s": round(float(duration_s), 4),
        "status": status,
        "thread": threading.current_thread().name,
    }
    if error:
        rec["error"] = redact_text(str(error))[:300]
    if meta:
        rec.update(redact(meta))
    _STATE.record_op(rec)


@contextlib.contextmanager
def timed(name: str, **meta: Any):
    """Context manager timing a SYNCHRONOUS operation. Re-raises on error."""
    start = time.monotonic()
    status = STATUS_OK
    err: Optional[str] = None
    try:
        yield
    except BaseException as exc:  # noqa: BLE001 - record then re-raise
        status = STATUS_ERROR
        err = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        record_op(name, time.monotonic() - start, status=status, error=err, **meta)


class atimed:
    """Async context manager timing an AWAITED operation. Re-raises on error.

    A `TimeoutError` / `asyncio.TimeoutError` is recorded with status
    ``timeout`` so wedged sockets are visually distinct from other failures.
    """

    def __init__(self, name: str, **meta: Any):
        self.name = name
        self.meta = meta
        self._start = 0.0

    async def __aenter__(self):
        self._start = time.monotonic()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        status = STATUS_OK
        err = None
        if exc is not None:
            err = f"{exc_type.__name__}: {exc}"
            status = STATUS_TIMEOUT if issubclass(exc_type, TimeoutError) else STATUS_ERROR
        record_op(self.name, time.monotonic() - self._start,
                  status=status, error=err, **self.meta)
        return False  # never suppress


def timed_async(name: str):
    """Decorator timing an async function via :class:`atimed`."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            async with atimed(name):
                return await fn(*args, **kwargs)
        return wrapper
    return deco


# ---------------------------------------------------------------------------
# Event-loop lag monitor
# ---------------------------------------------------------------------------

class EventLoopLagMonitor:
    """Detects when an asyncio loop fails to wake on schedule.

    Sleeps ``interval`` seconds in a loop; if the wall-clock gap materially
    exceeds ``interval`` the loop was blocked (something ran too long without
    yielding), which is the direct signature of the stalls under investigation.
    """

    def __init__(self, interval: float = 5.0, threshold_s: float = 2.0,
                 name: str = "loop"):
        self.interval = interval
        self.threshold_s = threshold_s
        self.name = name
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        import asyncio
        loop = asyncio.get_event_loop()
        _STATE.register_loop(loop)
        while not self._stop:
            expected = time.monotonic() + self.interval
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break
            lag = time.monotonic() - expected
            if lag >= self.threshold_s:
                _STATE.record_lag({
                    "t": datetime.now(timezone.utc).isoformat(),
                    "ts": round(time.time(), 3),
                    "monitor": self.name,
                    "expected_interval_s": self.interval,
                    "lag_s": round(lag, 3),
                    "thread": threading.current_thread().name,
                })


# ---------------------------------------------------------------------------
# CPU / memory sampling
# ---------------------------------------------------------------------------

def _resource_sample() -> Dict[str, Any]:
    sample: Dict[str, Any] = {
        "pid": os.getpid(),
        "num_threads": threading.active_count(),
        "num_frames": len(sys._current_frames()),
    }
    try:
        sample["loadavg"] = os.getloadavg()
    except (OSError, AttributeError):
        pass
    try:
        import resource
        ru = resource.getrusage(resource.RUSAGE_SELF)
        sample["ru_maxrss_kb"] = ru.ru_maxrss
        sample["ru_utime_s"] = round(ru.ru_utime, 3)
        sample["ru_stime_s"] = round(ru.ru_stime, 3)
    except (ImportError, ValueError):
        pass
    try:
        with open(f"/proc/{os.getpid()}/status") as f:
            for line in f:
                if line.startswith(("VmRSS", "VmSize", "Threads")):
                    key, _, val = line.partition(":")
                    sample[key.strip()] = val.strip()
    except OSError:
        pass
    return sample


# ---------------------------------------------------------------------------
# Thread-stack and asyncio-task capture
# ---------------------------------------------------------------------------

def _thread_stacks() -> List[Dict[str, Any]]:
    names = {t.ident: t.name for t in threading.enumerate()}
    out: List[Dict[str, Any]] = []
    for tid, frame in sys._current_frames().items():
        stack = traceback.format_stack(frame)
        out.append({
            "thread_id": tid,
            "thread_name": names.get(tid, "?"),
            "stack": [redact_text(line.rstrip()) for line in stack][-40:],
        })
    return out


def _asyncio_tasks() -> List[Dict[str, Any]]:
    import asyncio
    out: List[Dict[str, Any]] = []
    for loop in _STATE.loops():
        try:
            tasks = asyncio.all_tasks(loop)
        except RuntimeError:
            continue
        for task in list(tasks)[:100]:
            info: Dict[str, Any] = {
                "name": getattr(task, "get_name", lambda: "?")(),
                "done": task.done(),
                "coro": redact_text(repr(task.get_coro()))[:200],
            }
            try:
                frames = task.get_stack(limit=8)
                info["stack"] = [
                    redact_text(line.rstrip())
                    for f in frames
                    for line in traceback.format_stack(f)
                ][-20:]
            except Exception:  # noqa: BLE001
                pass
            out.append(info)
    return out


# ---------------------------------------------------------------------------
# Rich JSON dump
# ---------------------------------------------------------------------------

def dump_diagnostics(reason: str = "manual",
                     dump_dir: Optional[str] = None) -> Optional[str]:
    """Write a redacted JSON snapshot of the whole process. Returns the path."""
    try:
        base = Path(dump_dir) if dump_dir else _STATE.dump_dir
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = base / f"stall_diag_{stamp}_{os.getpid()}.json"
        payload = {
            "schema": "runtime-diag-1.0",
            "reason": reason,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "resources": _resource_sample(),
            "recent_ops": _STATE.snapshot_ops(),
            "recent_loop_lags": _STATE.snapshot_lags(),
            "thread_stacks": _thread_stacks(),
            "asyncio_tasks": _asyncio_tasks(),
        }
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=1, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return str(path)
    except Exception:  # noqa: BLE001 - diagnostics must never crash the bot
        return None


# ---------------------------------------------------------------------------
# Signal installation
# ---------------------------------------------------------------------------

def install_diagnostics(dump_dir: str = DEFAULT_DUMP_DIR,
                        native_signal: str = "SIGUSR1",
                        rich_signal: str = "SIGUSR2") -> Dict[str, Any]:
    """Install on-demand diagnostic dumps.

    * ``native_signal`` (default SIGUSR1): registers ``faulthandler`` for a
      C-level all-thread traceback appended to ``<dump_dir>/faulthandler.log``.
      Works even if the interpreter is wedged holding the GIL.
    * ``rich_signal`` (default SIGUSR2): a Python handler that writes the rich
      JSON snapshot from :func:`dump_diagnostics`.

    Fail-safe: unsupported signals (e.g. on Windows) are skipped silently.
    Returns a small status dict for logging.
    """
    import signal as _signal

    status: Dict[str, Any] = {"native": False, "rich": False, "dump_dir": dump_dir}
    base = Path(dump_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
        _STATE.dump_dir = base
    except OSError:
        return status

    # Native faulthandler dump (best resilience under a wedged interpreter).
    try:
        import faulthandler
        sig = getattr(_signal, native_signal, None)
        if sig is not None:
            fault_path = base / "faulthandler.log"
            # Keep the file object alive for the process lifetime.
            _STATE._fault_file = open(fault_path, "a")
            faulthandler.register(sig, file=_STATE._fault_file,
                                  all_threads=True, chain=False)
            status["native"] = True
            status["native_file"] = str(fault_path)
    except Exception:  # noqa: BLE001
        pass

    # Rich JSON dump via a Python signal handler.
    try:
        sig = getattr(_signal, rich_signal, None)
        if sig is not None:
            def _rich_handler(signum, frame):  # noqa: ANN001
                dump_diagnostics(reason=f"signal:{rich_signal}")
            _signal.signal(sig, _rich_handler)
            status["rich"] = True
    except (ValueError, OSError, RuntimeError):
        pass

    return status


def register_loop(loop: Any) -> None:
    """Register an asyncio loop so dumps can enumerate its tasks."""
    _STATE.register_loop(loop)
