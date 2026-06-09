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
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional

project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# Watchdog configuration
HEARTBEAT_FILE = "logs/bot_heartbeat.json"
JOURNAL_DIR = "logs"
SHADOW_DIR = "logs"
STALE_THRESHOLD_SECONDS = 45 * 60  # 45 minutes
WATCHDOG_CHECK_INTERVAL = 60  # Check every 60 seconds


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

        # Check heartbeat freshness
        heartbeat_age = self._get_file_age(HEARTBEAT_FILE)
        if heartbeat_age is not None and heartbeat_age > self.stale_threshold:
            print(f"[WATCHDOG] Heartbeat stale for {heartbeat_age:.0f}s (threshold: {self.stale_threshold}s)")
            self._restart_child(child_pid, "heartbeat_stale")
            return

        # Check journal freshness
        journal_age = self._get_newest_file_age(JOURNAL_DIR, "trade_journal_*.jsonl")
        if journal_age is not None and journal_age > self.stale_threshold * 2:
            print(f"[WATCHDOG] Journal stale for {journal_age:.0f}s")
            self._restart_child(child_pid, "journal_stale")
            return

    def _get_file_age(self, filepath: str) -> Optional[float]:
        """Get age of a file in seconds. Returns None if file doesn't exist."""
        try:
            path = project_root / filepath
            if path.exists():
                mtime = path.stat().st_mtime
                return time.time() - mtime
        except Exception:
            pass
        return None

    def _get_newest_file_age(self, directory: str, pattern: str) -> Optional[float]:
        """Get age of the newest file matching pattern in seconds."""
        try:
            dir_path = project_root / directory
            if not dir_path.exists():
                return None
            files = sorted(dir_path.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
            if files:
                return time.time() - files[0].stat().st_mtime
        except Exception:
            pass
        return None

    def _restart_child(self, child_pid: int, reason: str):
        """Kill the stuck child process."""
        now = time.time()
        if (now - self._last_restart_time) < self._restart_cooldown:
            print(f"[WATCHDOG] Cooldown active, skipping restart")
            return

        print(f"[WATCHDOG] Restarting child (reason: {reason})")
        self._last_restart_time = now

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

    print("=" * 80)
    print("BTC 15-MIN TRADING BOT - AUTO-RESTART WRAPPER")
    print("=" * 80)
    print(f"Platform: {sys.platform}")
    print(f"Python: {python_cmd}")
    print(f"Bot script: {BOT_SCRIPT}")
    print(f"Bot arguments: {bot_args}")
    print(f"Virtual env: {sys.prefix}")
    print(f"Watchdog: ENABLED (stale threshold: {STALE_THRESHOLD_SECONDS}s)")
    print("=" * 80)
    print()

    if not os.path.exists(BOT_SCRIPT):
        print(f"ERROR: Bot script '{BOT_SCRIPT}' not found!")
        sys.exit(1)

    restart_count = 0
    current_child_pid = [None]  # type: list[Optional[int]]

    def get_child_pid():
        return current_child_pid[0]

    # Start runner-level watchdog
    watchdog = RunnerWatchdog(child_pid_func=get_child_pid)
    watchdog.start()

    try:
        while True:
            restart_count += 1
            current_child_pid[None] = None

            print("=" * 80)
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
            print(f"Starting bot (restart #{restart_count})...")
            print(f"Command: {python_cmd} {BOT_SCRIPT} {' '.join(bot_args)}")
            print("=" * 80)
            print()

            try:
                cmd = [python_cmd, BOT_SCRIPT] + bot_args
                process = subprocess.Popen(cmd)
                current_child_pid[0] = process.pid
                print(f"[RUNNER] Child PID: {process.pid}")

                exit_code = process.wait()
                current_child_pid[0] = None

                print()
                print("=" * 80)
                print(f"Bot stopped at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"Exit code: {exit_code}")
                print("=" * 80)

                if exit_code in [0, 143, 15, -15]:
                    print("Normal auto-restart - loading fresh filters...")
                    wait_time = 2
                else:
                    print(f"Error detected (code {exit_code}) - waiting before retry...")
                    wait_time = 10

                print(f"Restarting in {wait_time} seconds...")
                print()
                time.sleep(wait_time)

            except KeyboardInterrupt:
                print()
                print("=" * 80)
                print("Keyboard interrupt received - stopping wrapper")
                print("=" * 80)
                break

            except Exception as e:
                print()
                print("=" * 80)
                print(f"ERROR running bot: {e}")
                print("=" * 80)
                print("Waiting 10 seconds before retry...")
                print()
                time.sleep(10)

    finally:
        watchdog.stop()

if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\nStopped by user")
        sys.exit(0)
