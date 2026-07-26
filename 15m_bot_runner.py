"""
BTC 15-Min Trading Bot - Auto-Restart Wrapper with Runner-Level Watchdog

Monitors child bot process health externally:
- Child process alive status
- Heartbeat file freshness
- Journal/shadow file freshness

If heartbeat is stale beyond threshold, kills child and lets runner restart it.
"""
import subprocess
import time
import sys
import os
import json
import signal
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from core.diagnostics import run_provenance  # noqa: E402

# Watchdog configuration
HEARTBEAT_FILE = "logs/bot_heartbeat.json"
JOURNAL_DIR = "logs"
SHADOW_DIR = "logs"
STALE_THRESHOLD_SECONDS = 45 * 60  # 45 minutes
WATCHDOG_CHECK_INTERVAL = 60  # Check every 60 seconds

# Directory where the child writes on-demand stall diagnostics (see
# core/diagnostics/runtime_diag.py). Before force-restarting a stalled child,
# the watchdog signals it to dump native + rich diagnostics here so the stall
# stack is captured while the process is still wedged.
DIAG_DUMP_DIR = "logs/diagnostics"
DIAG_CAPTURE_WAIT_SECONDS = 3.0

# Structured lifecycle event log (append-only, source of truth for run summaries)
RUNNER_EVENTS_FILE = "logs/runner_events.jsonl"

# The child bot performs a planned self-restart after this many minutes
# (see bot.py `restart_after_minutes`). Used to classify signal-terminated exits.
PLANNED_RESTART_MINUTES = 90
# Tolerance (seconds) around the planned-restart boundary when classifying.
PLANNED_RESTART_TOLERANCE_SECONDS = 5 * 60


class RunnerShutdown(BaseException):
    """
    Raised from a signal handler (SIGTERM/SIGINT/SIGHUP) to trigger a graceful
    runner shutdown. Inherits from BaseException (not Exception) so the main
    loop's generic ``except Exception`` cannot swallow it as a loop error.
    """

    def __init__(self, signum: Optional[int] = None):
        self.signum = signum
        super().__init__(f"runner shutdown on signal {signum}")


def _signal_name(signum: Optional[int]) -> Optional[str]:
    if signum is None:
        return None
    try:
        return signal.Signals(signum).name
    except (ValueError, AttributeError):
        return str(signum)


def _install_signal_handlers() -> None:
    """
    Install graceful-shutdown handlers for SIGTERM/SIGINT/SIGHUP.

    The handler simply raises RunnerShutdown in the main thread; the run loop
    performs the actual child termination and logging. Best-effort: signals not
    available on the platform (e.g. SIGHUP on Windows) are skipped.
    """
    def _handler(signum, frame):
        raise RunnerShutdown(signum)

    for _sig in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, _sig, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, RuntimeError):
            # e.g. not in the main thread, or unsupported on this platform.
            pass


def log_runner_event(event_type: str, **fields) -> None:
    """
    Append a single structured lifecycle event to RUNNER_EVENTS_FILE.

    Fail-safe: never raises. Best-effort persistence only.
    """
    record = {
        "ts": round(time.time(), 3),
        "t": datetime.now(timezone.utc).isoformat(),
        "event_type": event_type,
    }
    record.update(fields)
    try:
        path = project_root / RUNNER_EVENTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def classify_exit(
    exit_code: Optional[int],
    child_uptime_s: float,
    watchdog_triggered: bool,
    planned_restart_minutes: int = PLANNED_RESTART_MINUTES,
    planned_tolerance_s: int = PLANNED_RESTART_TOLERANCE_SECONDS,
) -> str:
    """
    Classify why a child bot process exited.

    Returns one of:
      - "watchdog_restart"  : the runner watchdog killed the child
      - "planned_restart"   : the bot self-terminated near its planned restart age
      - "external_sigterm"  : signal-terminated early, not by our watchdog
      - "clean_exit"        : exited 0 (unexpected for this bot's main loop)
      - "crash_error"       : non-zero/abnormal exit

    Notes:
      Signal termination shows up as a negative code (e.g. -15) from
      subprocess.wait(), or 143 (128+SIGTERM) when shell-wrapped. Exit code
      alone cannot distinguish planned vs watchdog vs external, so we use the
      watchdog flag and the child's uptime.
    """
    signal_terminated = exit_code in (-15, 15, 143)
    if watchdog_triggered:
        return "watchdog_restart"
    if signal_terminated:
        planned_floor = planned_restart_minutes * 60 - planned_tolerance_s
        if child_uptime_s >= planned_floor:
            return "planned_restart"
        return "external_sigterm"
    if exit_code == 0:
        return "clean_exit"
    return "crash_error"


class RunnerWatchdog:
    """
    External watchdog that monitors child bot health.
    Runs in a separate thread inside the runner process.
    """

    def __init__(self, child_pid_func, stale_threshold: int = STALE_THRESHOLD_SECONDS):
        self.child_pid_func = child_pid_func  # callable that returns current child PID
        self.stale_threshold = stale_threshold
        self._stop_event = threading.Event()
        self._thread = None
        self._last_restart_time = 0
        self._restart_cooldown = 120  # Minimum 2 minutes between restarts
        # Timestamp marking the start of the current run session. Journal/shadow
        # staleness is only evaluated against files written at/after this time,
        # so stale files from PREVIOUS runs/days never trigger a restart.
        self.run_start_ts: Optional[float] = None
        # Records the most recent watchdog-initiated restart so the main loop
        # can attribute a child exit to the watchdog (keyed by child PID).
        self.last_restart = {"pid": None, "ts": 0.0, "reason": None}

    def set_run_start(self, ts: float) -> None:
        """Mark the current run session start (used to scope journal staleness)."""
        self.run_start_ts = ts

    def start(self):
        """Start the watchdog monitoring thread."""
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        print(f"[WATCHDOG] Started (stale threshold: {self.stale_threshold}s)")

    def stop(self):
        """Stop the watchdog."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _monitor_loop(self):
        """Main monitoring loop."""
        while not self._stop_event.is_set():
            try:
                self._check_health()
            except Exception as e:
                print(f"[WATCHDOG] Error in health check: {e}")
            self._stop_event.wait(WATCHDOG_CHECK_INTERVAL)

    def _check_health(self):
        """Check if child bot is healthy."""
        child_pid = self.child_pid_func()
        if child_pid is None:
            return  # No child running, runner will restart

        # Check if child process is alive
        try:
            os.kill(child_pid, 0)  # Signal 0 = check if alive
        except ProcessLookupError:
            print(f"[WATCHDOG] Child PID {child_pid} not found")
            return
        except PermissionError:
            pass  # Process exists but we can't signal it

        # Check heartbeat freshness. Only a heartbeat written by the CURRENT
        # run session counts; a leftover heartbeat from a PREVIOUS run is
        # ignored so it cannot trigger a false heartbeat_stale restart while the
        # child is still bootstrapping (before it writes its first fresh
        # heartbeat). This mirrors the journal/shadow staleness scoping below.
        # Real stalls are still caught: once a fresh heartbeat exists staleness
        # applies normally, and a child that never produces a fresh heartbeat
        # within a full stale window after run start is restarted as a genuine
        # startup stall (heartbeat_never_fresh) rather than silently ignored.
        heartbeat_age = self._get_file_age(
            HEARTBEAT_FILE, min_mtime=self.run_start_ts
        )
        if heartbeat_age is not None and heartbeat_age > self.stale_threshold:
            print(f"[WATCHDOG] Heartbeat stale for {heartbeat_age:.0f}s (threshold: {self.stale_threshold}s)")
            self._restart_child(child_pid, "heartbeat_stale")
            return
        if heartbeat_age is None and self.run_start_ts is not None:
            startup_age = time.time() - self.run_start_ts
            if startup_age > self.stale_threshold:
                print(f"[WATCHDOG] No fresh heartbeat {startup_age:.0f}s after run start (threshold: {self.stale_threshold}s)")
                self._restart_child(child_pid, "heartbeat_never_fresh")
                return

        # Check journal freshness. Only consider journal files from the CURRENT
        # run session; files from previous runs/days are ignored. If no
        # current-run journal exists yet, skip this check (the heartbeat above
        # is the primary liveness signal) to avoid a false journal_stale loop.
        journal_age = self._get_newest_file_age(
            JOURNAL_DIR, "trade_journal_*.jsonl", min_mtime=self.run_start_ts
        )
        if journal_age is not None and journal_age > self.stale_threshold * 2:
            print(f"[WATCHDOG] Journal stale for {journal_age:.0f}s")
            self._restart_child(child_pid, "journal_stale")
            return

    def _get_file_age(self, filepath: str, min_mtime: Optional[float] = None) -> Optional[float]:
        """Get age of a file in seconds. Returns None if the file doesn't exist,
        or — when ``min_mtime`` is given — if it was last modified before
        ``min_mtime`` (used to ignore stale files left by previous run
        sessions)."""
        try:
            path = project_root / filepath
            if path.exists():
                mtime = path.stat().st_mtime
                if min_mtime is not None and mtime < min_mtime:
                    return None
                return time.time() - mtime
        except Exception:
            pass
        return None

    def _get_newest_file_age(
        self, directory: str, pattern: str, min_mtime: Optional[float] = None
    ) -> Optional[float]:
        """
        Get age (seconds) of the newest file matching pattern.

        If ``min_mtime`` is provided, only files modified at/after that time are
        considered (used to ignore stale files from previous run sessions).
        Returns None when no qualifying file exists.
        """
        try:
            dir_path = project_root / directory
            if not dir_path.exists():
                return None
            candidates = []
            for f in dir_path.glob(pattern):
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                if min_mtime is not None and mtime < min_mtime:
                    continue
                candidates.append(mtime)
            if candidates:
                return time.time() - max(candidates)
        except Exception:
            pass
        return None

    def _capture_child_diagnostics(self, child_pid: int) -> dict:
        """
        Signal the wedged child to dump diagnostics BEFORE we terminate it.

        Sends SIGUSR1 (native faulthandler all-thread traceback) and SIGUSR2
        (rich JSON snapshot), waits briefly for the files to be written, then
        reports the newest diagnostic artifacts. Best-effort and fail-safe.
        """
        result = {"faulthandler_log": None, "rich_dump": None, "signalled": False}
        dump_dir = project_root / DIAG_DUMP_DIR
        # Record which rich dumps already exist so we can detect the new one.
        before = set()
        try:
            before = {p.name for p in dump_dir.glob("stall_diag_*.json")}
        except OSError:
            pass

        for signame in ("SIGUSR1", "SIGUSR2"):
            sig = getattr(signal, signame, None)
            if sig is None:
                continue
            try:
                os.kill(child_pid, sig)
                result["signalled"] = True
            except ProcessLookupError:
                print(f"[WATCHDOG] Child gone before diagnostics ({signame})")
                return result
            except Exception as e:  # noqa: BLE001
                print(f"[WATCHDOG] Failed to send {signame}: {e}")

        # Give the child a moment to write the dumps.
        time.sleep(DIAG_CAPTURE_WAIT_SECONDS)

        try:
            fault_log = dump_dir / "faulthandler.log"
            if fault_log.exists():
                result["faulthandler_log"] = str(fault_log.relative_to(project_root))
            new_dumps = sorted(
                p for p in dump_dir.glob("stall_diag_*.json") if p.name not in before
            )
            if new_dumps:
                result["rich_dump"] = str(new_dumps[-1].relative_to(project_root))
        except OSError:
            pass

        print(f"[WATCHDOG] Captured child diagnostics: {result}")
        return result

    def _restart_child(self, child_pid: int, reason: str):
        """Kill the stuck child process."""
        now = time.time()
        if (now - self._last_restart_time) < self._restart_cooldown:
            print(f"[WATCHDOG] Cooldown active, skipping restart")
            return

        # Gather diagnostic info before restart (scoped to the current run
        # session so the logged ages match what the health check evaluates).
        heartbeat_age = self._get_file_age(HEARTBEAT_FILE)
        journal_age = self._get_newest_file_age(
            JOURNAL_DIR, "trade_journal_*.jsonl", min_mtime=self.run_start_ts
        )
        shadow_age = self._get_newest_file_age(
            SHADOW_DIR, "shadow_decisions_*.jsonl", min_mtime=self.run_start_ts
        )

        print(f"[WATCHDOG] === RESTART DIAGNOSTICS ===")
        print(f"[WATCHDOG] Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"[WATCHDOG] Old child PID: {child_pid}")
        print(f"[WATCHDOG] Reason: {reason}")
        print(f"[WATCHDOG] Heartbeat age: {heartbeat_age:.0f}s" if heartbeat_age else "[WATCHDOG] Heartbeat age: unknown")
        print(f"[WATCHDOG] Journal age: {journal_age:.0f}s" if journal_age else "[WATCHDOG] Journal age: unknown")
        print(f"[WATCHDOG] Shadow age: {shadow_age:.0f}s" if shadow_age else "[WATCHDOG] Shadow age: unknown")
        print(f"[WATCHDOG] === END DIAGNOSTICS ===")

        self._last_restart_time = now
        # Before terminating, ask the wedged child to dump its diagnostics
        # (native all-thread traceback + rich JSON snapshot) so the stall stack
        # is captured while the process is still stuck.
        diag_paths = self._capture_child_diagnostics(child_pid)

        # Attribute this child's upcoming exit to the watchdog (by PID).
        self.last_restart = {"pid": child_pid, "ts": now, "reason": reason}
        log_runner_event(
            "watchdog_restart",
            child_pid=child_pid,
            reason=reason,
            heartbeat_age_s=round(heartbeat_age, 1) if heartbeat_age is not None else None,
            journal_age_s=round(journal_age, 1) if journal_age is not None else None,
            shadow_age_s=round(shadow_age, 1) if shadow_age is not None else None,
            diagnostics=diag_paths,
        )

        # Try graceful termination first
        try:
            os.kill(child_pid, 15)  # SIGTERM
            print(f"[WATCHDOG] Sent SIGTERM to PID {child_pid}")
            time.sleep(10)  # Wait for graceful shutdown
        except ProcessLookupError:
            print(f"[WATCHDOG] Child already terminated")
            return
        except Exception as e:
            print(f"[WATCHDOG] SIGTERM failed: {e}")

        # Check if still alive, force kill if needed
        try:
            os.kill(child_pid, 0)
            print(f"[WATCHDOG] Child still alive, sending SIGKILL")
            os.kill(child_pid, 9)  # SIGKILL
            time.sleep(2)
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"[WATCHDOG] SIGKILL failed: {e}")

        print(f"[WATCHDOG] Child terminated, runner will restart")


def run_bot():
    """Run the bot with auto-restart and runner-level watchdog."""

    BOT_SCRIPT = "bot.py"
    python_cmd = sys.executable
    bot_args = sys.argv[1:] if len(sys.argv) > 1 else []

    # --- Run provenance (computed ONCE here, never per child/record) ---
    runner_start_ts = time.time()
    dry_run = "--live" not in bot_args
    live_trading_ack = os.getenv("LIVE_TRADING_ACK", "").lower() == "true"
    try:
        from core.strategy_brain.shadow_evaluator import SHADOW_SCHEMA_VERSION as _schema_version
    except Exception:
        _schema_version = None
    provenance = run_provenance.compute_provenance(
        repo_root=project_root,
        bot_args=bot_args,
        dry_run=dry_run,
        live_trading_ack=live_trading_ack,
        runner_pid=os.getpid(),
        runner_start_ts=runner_start_ts,
        schema_version=_schema_version,
    )
    manifest_path = run_provenance.write_start_manifest(project_root, provenance)
    _git = provenance["git"]
    _head_short = _git["head"][:12] if _git["head"] != run_provenance.UNAVAILABLE else run_provenance.UNAVAILABLE
    child_env = {**os.environ, **run_provenance.env_for_child(provenance)}

    print("=" * 80)
    print("BTC 15-MIN TRADING BOT - AUTO-RESTART WRAPPER")
    print("=" * 80)
    print(f"Platform: {sys.platform}")
    print(f"Python: {python_cmd}")
    print(f"Bot script: {BOT_SCRIPT}")
    print(f"Bot arguments: {bot_args}")
    print(f"Virtual env: {sys.prefix}")
    print(f"Watchdog: ENABLED (stale threshold: {STALE_THRESHOLD_SECONDS}s)")
    print(f"Run provenance: run_id={provenance['run_id']} git_head={_head_short} "
          f"dirty={_git['dirty']} dry_run={dry_run}")
    print(f"Run manifest: {manifest_path}")
    print("=" * 80)
    print()

    if not os.path.exists(BOT_SCRIPT):
        print(f"ERROR: Bot script '{BOT_SCRIPT}' not found!")
        sys.exit(1)

    restart_count = 0
    current_child_pid = [None]  # type: list[Optional[int]]

    def get_child_pid():
        return current_child_pid[0]

    # Tracks the currently-running child so the shutdown path can terminate it
    # gracefully and log a child_exit even when interrupted by a signal.
    active_child = {"process": None, "start_ts": None, "restart_index": 0}

    # Running lifecycle totals for this run session, folded into the end
    # manifest at shutdown (a dict, not locals, so both the main loop and
    # the nested _shutdown_child() below can update it without `nonlocal`).
    lifecycle_counts = {
        "child_start": 0, "child_exit": 0,
        "clean_exit": 0, "watchdog_restart": 0,
    }

    def _shutdown_child() -> None:
        """Terminate the running child gracefully and log its child_exit."""
        proc = active_child["process"]
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()  # SIGTERM
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    print("[RUNNER] Child did not exit on SIGTERM; sending SIGKILL")
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            except Exception as e:
                print(f"[RUNNER] Error terminating child during shutdown: {e}")
        uptime = time.time() - (active_child["start_ts"] or time.time())
        log_runner_event(
            "child_exit",
            child_pid=proc.pid,
            restart_index=active_child["restart_index"],
            exit_code=proc.returncode,
            child_uptime_s=round(uptime, 1),
            watchdog_triggered=False,
            classification="runner_shutdown",
        )
        lifecycle_counts["child_exit"] += 1
        active_child["process"] = None
        current_child_pid[0] = None

    # Install graceful-shutdown signal handlers (SIGTERM/SIGINT/SIGHUP).
    _install_signal_handlers()

    # Start runner-level watchdog
    watchdog = RunnerWatchdog(child_pid_func=get_child_pid)
    watchdog.set_run_start(time.time())
    watchdog.start()

    log_runner_event("runner_start", pid=os.getpid(), bot_args=bot_args)

    shutdown_signal: Optional[int] = None
    try:
        while True:
            restart_count += 1
            current_child_pid[0] = None

            print("=" * 80)
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
            print(f"Starting bot (restart #{restart_count})...")
            print(f"Command: {python_cmd} {BOT_SCRIPT} {' '.join(bot_args)}")
            print("=" * 80)
            print()

            try:
                cmd = [python_cmd, BOT_SCRIPT] + bot_args
                process = subprocess.Popen(cmd, env=child_env)
                current_child_pid[0] = process.pid
                print(f"[RUNNER] Child PID: {process.pid}")

                child_start_ts = time.time()
                active_child["process"] = process
                active_child["start_ts"] = child_start_ts
                active_child["restart_index"] = restart_count
                log_runner_event(
                    "child_start",
                    child_pid=process.pid,
                    restart_index=restart_count,
                )
                lifecycle_counts["child_start"] += 1

                exit_code = process.wait()
                active_child["process"] = None
                current_child_pid[0] = None
                child_exit_ts = time.time()
                child_uptime_s = child_exit_ts - child_start_ts

                # Attribute the exit: did our watchdog kill THIS child?
                watchdog_triggered = (
                    watchdog.last_restart.get("pid") == process.pid
                )
                classification = classify_exit(
                    exit_code=exit_code,
                    child_uptime_s=child_uptime_s,
                    watchdog_triggered=watchdog_triggered,
                )
                log_runner_event(
                    "child_exit",
                    child_pid=process.pid,
                    restart_index=restart_count,
                    exit_code=exit_code,
                    child_uptime_s=round(child_uptime_s, 1),
                    watchdog_triggered=watchdog_triggered,
                    watchdog_reason=watchdog.last_restart.get("reason") if watchdog_triggered else None,
                    classification=classification,
                )
                lifecycle_counts["child_exit"] += 1
                if classification == "clean_exit":
                    lifecycle_counts["clean_exit"] += 1
                elif classification == "watchdog_restart":
                    lifecycle_counts["watchdog_restart"] += 1

                print()
                print("=" * 80)
                print(f"Bot stopped at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"Exit code: {exit_code} | uptime: {child_uptime_s:.0f}s | cause: {classification}")
                print("=" * 80)

                if classification == "crash_error":
                    print(f"Error detected (code {exit_code}) - waiting before retry...")
                    wait_time = 10
                else:
                    print(f"{classification} - loading fresh filters...")
                    wait_time = 2

                print(f"Restarting in {wait_time} seconds...")
                print()
                time.sleep(wait_time)

            except KeyboardInterrupt:
                # Defer to the outer handler so the child is shut down
                # gracefully and runner_stop is logged exactly once.
                raise

            except Exception as e:
                print()
                print("=" * 80)
                print(f"ERROR running bot: {e}")
                print("=" * 80)
                print("Waiting 10 seconds before retry...")
                print()
                log_runner_event("runner_loop_error", error=str(e)[:200])
                time.sleep(10)

    except RunnerShutdown as e:
        shutdown_signal = e.signum
        sig_name = _signal_name(shutdown_signal)
        print()
        print("=" * 80)
        print(f"Received {sig_name or 'shutdown signal'} - stopping runner gracefully")
        print("=" * 80)
        _shutdown_child()

    except KeyboardInterrupt:
        print()
        print("=" * 80)
        print("Keyboard interrupt received - stopping runner gracefully")
        print("=" * 80)
        _shutdown_child()

    finally:
        watchdog.stop()
        log_runner_event(
            "runner_stop",
            pid=os.getpid(),
            signal=_signal_name(shutdown_signal),
        )
        try:
            run_provenance.write_end_manifest(
                repo_root=project_root,
                run_id=provenance["run_id"],
                stop_reason=_signal_name(shutdown_signal) or "unknown",
                child_start_count=lifecycle_counts["child_start"],
                child_exit_count=lifecycle_counts["child_exit"],
                clean_exit_count=lifecycle_counts["clean_exit"],
                watchdog_restart_count=lifecycle_counts["watchdog_restart"],
                runner_start_ts=runner_start_ts,
            )
        except Exception as e:
            print(f"[RUNNER] Failed to write end manifest: {e}")

if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\nStopped by user")
        sys.exit(0)
