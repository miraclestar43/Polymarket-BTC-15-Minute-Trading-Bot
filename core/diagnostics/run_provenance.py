"""
Run provenance — DIAGNOSTIC / METADATA ONLY.

Captures a lightweight, immutable fingerprint of exactly what code and
config a run session executed: Git HEAD/branch/dirty-state, config/
dependency hashes, interpreter/host info, and the DRY_RUN/LIVE_TRADING_ACK
flags. This exists so a future audit can prove which records came from
which code version without guessing from wall-clock timestamps (see the
2026-07-20..27 run, where source files were edited while the runner was
still active between child restarts).

Design constraints (do not violate):
  - Computed ONCE per run session (by the runner, at startup) — never call
    Git or hash files per record.
  - Never logs secrets or raw environment contents — only derived metadata
    (a Git SHA, a config hash, a hostname, a bool). No .env values, no
    tokens, no private keys.
  - Never alters any strategy decision — pure metadata.
  - Never depends on remote Git access (local `git rev-parse`/`status`/
    `branch` only — no fetch/pull/ls-remote).
  - If Git (or any other input) is unavailable, records an explicit
    "unavailable" status rather than raising.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

UNAVAILABLE = "unavailable"

# Environment variable names used to propagate provenance from the runner
# (which computes it once) down to each child process (which just reads
# these — no Git calls, no file hashing in the child).
ENV_PREFIX = "POLY_RUN_"
_ENV_KEYS = (
    "RUN_ID", "GIT_HEAD", "GIT_BRANCH", "GIT_DIRTY",
    "STRATEGY_CONFIG_SHA256", "REQUIREMENTS_SHA256",
    "SCHEMA_VERSION", "APP_VERSION",
)


def _run_git(repo_root: Path, args: list, timeout: float = 5.0) -> Optional[str]:
    """Run a local, read-only git command; return stripped stdout or None on
    any failure (git missing, not a repo, timeout, non-zero exit)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root)] + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except Exception:
        return None


def compute_git_info(repo_root: Path) -> Dict[str, Any]:
    """Local-only Git metadata: HEAD, branch, dirty status. Never touches a
    remote. Any failure degrades to explicit "unavailable" values."""
    head = _run_git(repo_root, ["rev-parse", "HEAD"])
    branch = _run_git(repo_root, ["branch", "--show-current"])
    status = _run_git(repo_root, ["status", "--porcelain"])
    if head is None:
        return {"head": UNAVAILABLE, "branch": UNAVAILABLE, "dirty": UNAVAILABLE}
    return {
        "head": head,
        "branch": branch if branch is not None else UNAVAILABLE,
        # status is "" (clean) vs non-empty (dirty) vs None (git status itself failed)
        "dirty": UNAVAILABLE if status is None else bool(status),
    }


def sha256_file(path: Path) -> Optional[str]:
    """SHA256 of a file's bytes, or None if it doesn't exist / can't be read."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def make_run_id(runner_start_ts: float, runner_pid: int) -> str:
    """Deterministic, sortable, collision-resistant run id: tied to the
    runner's own start time and PID (both already the natural unique key
    used by runner_events.jsonl), no randomness required."""
    return f"run_{int(runner_start_ts)}_{runner_pid}"


def compute_provenance(
    repo_root: Path,
    bot_args: list,
    dry_run: bool,
    live_trading_ack: bool,
    runner_pid: int,
    runner_start_ts: float,
    schema_version: Optional[int] = None,
    app_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute the full provenance record ONCE, at runner startup."""
    git_info = compute_git_info(repo_root)
    strategy_config_hash = sha256_file(repo_root / "strategy.yaml")
    requirements_hash = sha256_file(repo_root / "requirements.txt")
    return {
        "run_id": make_run_id(runner_start_ts, runner_pid),
        "runner_start": time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(runner_start_ts)
        ) + f".{int((runner_start_ts % 1) * 1e6):06d}+00:00",
        "runner_pid": runner_pid,
        "git": git_info,
        "strategy_config_sha256": strategy_config_hash or UNAVAILABLE,
        "requirements_sha256": requirements_hash or UNAVAILABLE,
        "dependency_lock_source": "requirements.txt" if requirements_hash else UNAVAILABLE,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "schema_version": schema_version if schema_version is not None else UNAVAILABLE,
        "app_version": app_version or UNAVAILABLE,
        "bot_args": list(bot_args),
        "dry_run": bool(dry_run),
        "live_trading_ack": bool(live_trading_ack),
        "hostname": socket.gethostname(),
        "platform": sys.platform,
    }


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON atomically: write to a temp file in the same directory,
    then os.replace (atomic on POSIX) so a reader never observes a partial
    write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
            f.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def run_manifest_path(repo_root: Path, run_id: str) -> Path:
    return repo_root / "logs" / "runs" / run_id / "run_manifest.json"


def run_manifest_end_path(repo_root: Path, run_id: str) -> Path:
    return repo_root / "logs" / "runs" / run_id / "run_manifest_end.json"


def write_start_manifest(repo_root: Path, provenance: Dict[str, Any]) -> Path:
    path = run_manifest_path(repo_root, provenance["run_id"])
    write_json_atomic(path, provenance)
    return path


def write_end_manifest(
    repo_root: Path,
    run_id: str,
    stop_reason: Optional[str],
    child_start_count: int,
    child_exit_count: int,
    clean_exit_count: int,
    watchdog_restart_count: int,
    runner_start_ts: float,
) -> Path:
    """Separate, immutable end-of-run manifest (simpler and safer than
    rewriting the start manifest in place under signal-handler conditions)."""
    now = time.time()
    end = {
        "run_id": run_id,
        "end_timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
        + f".{int((now % 1) * 1e6):06d}+00:00",
        "stop_reason": stop_reason or "unknown",
        "runner_duration_s": round(now - runner_start_ts, 1),
        "child_start_count": child_start_count,
        "child_exit_count": child_exit_count,
        "clean_exit_count": clean_exit_count,
        "watchdog_restart_count": watchdog_restart_count,
    }
    path = run_manifest_end_path(repo_root, run_id)
    write_json_atomic(path, end)
    return path


def heartbeat_path(repo_root: Path, run_id: str) -> Path:
    return repo_root / "logs" / "runs" / run_id / "heartbeat.json"


def write_heartbeat(
    repo_root: Path,
    run_id: str,
    runner_pid: int,
    child_pid: int,
    child_generation: Any,
    child_start_ts: Optional[float],
    event: str,
    uptime_s: int,
    git_head: str,
    schema_version: Any,
) -> Path:
    """Run-owned heartbeat, written by the CHILD on every meaningful
    progress event (quote tick, timer cycle, journal write -- never on an
    independent timer, so a truly stuck event loop still stops producing
    heartbeats). Atomic write; the payload is a small flat dict (bounded
    size -- it is overwritten in place, never appended to). Carries enough
    identity for the watchdog to prove a heartbeat belongs to THIS run and
    THIS child before ever treating it as evidence of liveness."""
    now = time.time()
    payload = {
        "run_id": run_id,
        "runner_pid": runner_pid,
        "child_pid": child_pid,
        "child_generation": child_generation,
        "child_start_ts": child_start_ts,
        "timestamp": now,
        "event": event,
        "uptime_s": uptime_s,
        "git_head": git_head,
        "schema_version": schema_version,
    }
    path = heartbeat_path(repo_root, run_id)
    write_json_atomic(path, payload)
    return path


def read_heartbeat(repo_root: Path, run_id: str) -> Optional[Dict[str, Any]]:
    """Read the run-owned heartbeat, or None if it doesn't exist / can't be
    parsed (never raises -- a missing or corrupt heartbeat is exactly the
    "not yet proven alive" state the watchdog already has to handle)."""
    path = heartbeat_path(repo_root, run_id)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def watchdog_events_path(repo_root: Path, run_id: str) -> Path:
    return repo_root / "logs" / "runs" / run_id / "watchdog_events.jsonl"


def append_watchdog_event(repo_root: Path, run_id: str, record: Dict[str, Any]) -> Path:
    """Run-scoped watchdog-diagnostics index: a small, append-only mirror of
    this run's watchdog_restart events (which already carry diagnostics
    file paths), so a future audit of THIS run doesn't have to grep the
    shared logs/runner_events.jsonl for its run_id. Additive only -- the
    shared log is unchanged and remains the source of truth; this is a
    convenience index, not a migration."""
    path = watchdog_events_path(repo_root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
    return path


def run_abort_manifest_path(repo_root: Path, run_id: str) -> Path:
    return repo_root / "logs" / "runs" / run_id / "abort_manifest.json"


def write_abort_manifest(
    repo_root: Path,
    run_id: str,
    status: str,
    reason: str,
    reason_detail: str,
    start_timestamp: str,
    stop_timestamp: str,
    git_head: str,
    record_count: int,
    excluded_from_replication_metrics: bool = True,
) -> Path:
    """Separate, immutable classification manifest for a run that was
    aborted/disqualified after the fact (e.g. an isolation violation
    discovered post-hoc). Never mutates the original start/end manifests or
    any raw record — this is purely an additional, explicit classification
    record so a future audit does not have to re-derive why a run's data is
    excluded."""
    now = time.time()
    abort = {
        "run_id": run_id,
        "status": status,
        "start_timestamp": start_timestamp,
        "stop_timestamp": stop_timestamp,
        "reason": reason,
        "reason_detail": reason_detail,
        "git_head": git_head,
        "shadow_decision_record_count": record_count,
        "excluded_from_replication_metrics": excluded_from_replication_metrics,
        "classified_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
        + f".{int((now % 1) * 1e6):06d}+00:00",
    }
    path = run_abort_manifest_path(repo_root, run_id)
    write_json_atomic(path, abort)
    return path


def env_for_child(provenance: Dict[str, Any]) -> Dict[str, str]:
    """Environment-variable overlay to pass this run's provenance down to a
    freshly-spawned child process (merge onto os.environ, never replace
    it — the child still needs PATH, HOME, etc.)."""
    git = provenance.get("git", {})
    dirty = git.get("dirty")
    return {
        f"{ENV_PREFIX}RUN_ID": str(provenance.get("run_id", UNAVAILABLE)),
        f"{ENV_PREFIX}GIT_HEAD": str(git.get("head", UNAVAILABLE)),
        f"{ENV_PREFIX}GIT_BRANCH": str(git.get("branch", UNAVAILABLE)),
        f"{ENV_PREFIX}GIT_DIRTY": (
            UNAVAILABLE if dirty == UNAVAILABLE else ("true" if dirty else "false")
        ),
        f"{ENV_PREFIX}STRATEGY_CONFIG_SHA256": str(
            provenance.get("strategy_config_sha256", UNAVAILABLE)
        ),
        f"{ENV_PREFIX}REQUIREMENTS_SHA256": str(
            provenance.get("requirements_sha256", UNAVAILABLE)
        ),
        f"{ENV_PREFIX}SCHEMA_VERSION": str(provenance.get("schema_version", UNAVAILABLE)),
        f"{ENV_PREFIX}APP_VERSION": str(provenance.get("app_version", UNAVAILABLE)),
    }


_cached_child_provenance: Optional[Dict[str, Any]] = None


def get_current_run_provenance() -> Dict[str, Any]:
    """For use INSIDE a child process (e.g. shadow_evaluator.py): reads the
    provenance the runner already computed and passed via environment
    variables. Cheap (no subprocess, no file I/O beyond the first call) and
    cached after the first call so it is computed effectively once per
    process, never per record. Falls back to "unavailable" fields when run
    directly (e.g. bot.py started without 15m_bot_runner.py)."""
    global _cached_child_provenance
    if _cached_child_provenance is not None:
        return _cached_child_provenance
    env = os.environ
    dirty_raw = env.get(f"{ENV_PREFIX}GIT_DIRTY", UNAVAILABLE)
    dirty: Any
    if dirty_raw == "true":
        dirty = True
    elif dirty_raw == "false":
        dirty = False
    else:
        dirty = UNAVAILABLE
    _cached_child_provenance = {
        "run_id": env.get(f"{ENV_PREFIX}RUN_ID", UNAVAILABLE),
        "git_head": env.get(f"{ENV_PREFIX}GIT_HEAD", UNAVAILABLE),
        "git_branch": env.get(f"{ENV_PREFIX}GIT_BRANCH", UNAVAILABLE),
        "git_dirty": dirty,
        "strategy_config_sha256": env.get(
            f"{ENV_PREFIX}STRATEGY_CONFIG_SHA256", UNAVAILABLE
        ),
        "requirements_sha256": env.get(f"{ENV_PREFIX}REQUIREMENTS_SHA256", UNAVAILABLE),
    }
    return _cached_child_provenance


def reset_cache_for_tests() -> None:
    """Test-only: clear the module-level cache so a test can simulate a
    fresh child process observing new environment variables."""
    global _cached_child_provenance
    _cached_child_provenance = None
