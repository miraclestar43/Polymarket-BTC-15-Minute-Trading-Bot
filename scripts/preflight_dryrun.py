#!/usr/bin/env python3
"""
Preflight for a DRY-RUN shadow run — SAFETY GATE, read-only.

Verifies the environment is safe and ready BEFORE starting a dry-run, WITHOUT
exposing any secret values. It never places orders, never enables live trading,
and never prints keys/tokens/addresses.

Checks:
  * dry-run posture: config `dry_run=true` and `LIVE_TRADING_ACK` != true,
  * no `--live` in this invocation's arguments,
  * required directories exist / are writable (logs, logs/diagnostics),
  * key data files present with a recognisable schema,
  * network/DNS reachability of the CLOB host (DNS only — no API call),
  * safety service state (polymarket-dryrun.service) reported (not started),
  * diagnostic signal readiness (SIGUSR1/SIGUSR2 installable),
  * Git HEAD + working-tree cleanliness (report-only unless
    `--require-clean-git`, which turns a dirty tree into a blocking FAIL —
    for a replication run that must run one fixed commit start-to-finish),
  * required config hashes (strategy.yaml, requirements.txt) present,
  * no other `15m_bot_runner.py` process already running,
  * no `polymarket-collection-*.service` already active.

Exit 0 = safe to proceed. Non-zero = at least one blocking check failed.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.diagnostics import run_provenance as _rp  # noqa: E402

CLOB_HOST = "clob.polymarket.com"
REQUIRED_DIRS = ["logs", "logs/diagnostics"]

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


class Check:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.blocking_fail = False

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.rows.append((name, status, detail))
        if status == FAIL:
            self.blocking_fail = True

    def render(self) -> str:
        width = max(len(n) for n, _, _ in self.rows)
        lines = ["=== DRY-RUN PREFLIGHT ==="]
        for name, status, detail in self.rows:
            lines.append(f"  [{status}] {name.ljust(width)}  {detail}")
        lines.append("")
        lines.append("RESULT: " + ("BLOCKED" if self.blocking_fail else "OK — safe to proceed"))
        return "\n".join(lines)


def _check_dry_run(chk: Check) -> None:
    try:
        os.chdir(PROJECT_ROOT)
        from config import load_strategy_config  # local import; no secrets printed
        cfg = load_strategy_config()
        dry = bool(cfg.get("dry_run", True))
        ack = bool(cfg.get("live_trading_ack", False))
        chk.add("dry_run=true", PASS if dry else FAIL,
                "" if dry else "config dry_run is not true")
        # ack is derived only from LIVE_TRADING_ACK env; we report the boolean,
        # never the value of any secret.
        chk.add("LIVE_TRADING_ACK not true", PASS if not ack else FAIL,
                "" if not ack else "LIVE_TRADING_ACK=true would allow live orders")
    except Exception as e:  # noqa: BLE001
        chk.add("dry_run posture", FAIL, f"could not load config: {type(e).__name__}")


def _check_no_live_flag(chk: Check, argv: list[str]) -> None:
    live = "--live" in argv
    chk.add("no --live flag", PASS if not live else FAIL,
            "" if not live else "remove --live to stay in dry-run")


def _check_dirs(chk: Check) -> None:
    for rel in REQUIRED_DIRS:
        p = PROJECT_ROOT / rel
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".preflight_write_test"
            probe.write_text("ok")
            probe.unlink()
            chk.add(f"dir writable: {rel}", PASS)
        except OSError as e:
            chk.add(f"dir writable: {rel}", FAIL, str(e))


def _check_data_files(chk: Check) -> None:
    logs = PROJECT_ROOT / "logs"
    shadow = list(logs.glob("shadow_decisions_*.jsonl"))
    chk.add("shadow decision logs present",
            PASS if shadow else WARN,
            f"{len(shadow)} file(s)" if shadow else "none yet (ok for a fresh run)")
    store = logs / "official_resolutions.jsonl"
    chk.add("official resolution store",
            PASS if store.exists() else WARN,
            "present" if store.exists() else "absent (ok for a fresh run)")


def _check_dns(chk: Check) -> None:
    # DNS only — we do NOT open a socket to the API or send any request.
    try:
        socket.getaddrinfo(CLOB_HOST, 443)
        chk.add(f"DNS resolves {CLOB_HOST}", PASS)
    except socket.gaierror as e:
        chk.add(f"DNS resolves {CLOB_HOST}", WARN, f"resolution failed: {e}")


def _check_service(chk: Check) -> None:
    try:
        out = subprocess.run(
            ["systemctl", "is-active", "polymarket-dryrun.service"],
            capture_output=True, text=True, timeout=5)
        state = out.stdout.strip() or "unknown"
        # We only report state; we do NOT start/stop anything.
        chk.add("polymarket-dryrun.service state", PASS, state)
    except (OSError, subprocess.SubprocessError):
        chk.add("polymarket-dryrun.service state", WARN, "systemctl unavailable")


def _check_diag_signals(chk: Check) -> None:
    import signal as _sig
    have = all(getattr(_sig, s, None) is not None for s in ("SIGUSR1", "SIGUSR2"))
    chk.add("diagnostic signals available", PASS if have else WARN,
            "SIGUSR1/SIGUSR2" if have else "not on this platform")
    try:
        from core.diagnostics import runtime_diag as diag
        status = diag.install_diagnostics(
            dump_dir=str(PROJECT_ROOT / "logs" / "diagnostics"))
        ok = status.get("native") and status.get("rich")
        chk.add("diagnostics installable", PASS if ok else WARN,
                f"native={status.get('native')} rich={status.get('rich')}")
    except Exception as e:  # noqa: BLE001
        chk.add("diagnostics installable", WARN, f"{type(e).__name__}")


def _check_repo_and_venv(chk: Check) -> None:
    # Confirm we operate on the expected repository and its virtualenv.
    marker = PROJECT_ROOT / "15m_bot_runner.py"
    chk.add("repository layout", PASS if marker.exists() else FAIL,
            str(PROJECT_ROOT) if marker.exists() else "runner not found in this dir")
    venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
    using_venv = str(PROJECT_ROOT / ".venv") in str(Path(sys.executable).resolve())
    chk.add("virtual environment", PASS if (using_venv or venv_py.exists()) else WARN,
            "running under .venv" if using_venv else
            (".venv present (run via .venv/bin/python)" if venv_py.exists()
             else "no .venv detected"))


def _check_disk_space(chk: Check, min_free_mb: int = 500) -> None:
    try:
        st = os.statvfs(PROJECT_ROOT / "logs")
        free_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)
        status = PASS if free_mb >= min_free_mb else (WARN if free_mb >= 100 else FAIL)
        chk.add("disk space (logs partition)", status, f"{free_mb:.0f} MiB free")
    except OSError as e:
        chk.add("disk space (logs partition)", WARN, str(e))


def _check_clock_sync(chk: Check) -> None:
    # Read-only NTP/clock status via timedatectl; never adjusts the clock.
    try:
        out = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized"],
            capture_output=True, text=True, timeout=5)
        txt = (out.stdout or "").strip()
        synced = "NTPSynchronized=yes" in txt
        chk.add("system clock NTP sync", PASS if synced else WARN,
                "synchronized" if synced else "not synchronized / unknown")
    except (OSError, subprocess.SubprocessError):
        chk.add("system clock NTP sync", WARN, "timedatectl unavailable")


def _check_dualbook_config(chk: Check) -> None:
    try:
        os.chdir(PROJECT_ROOT)
        from config import load_strategy_config
        cfg = load_strategy_config()
        md = cfg.get("market_data") or {}
        chk.add("dual-book config present", PASS if md else WARN,
                "market_data section found" if md else "market_data section missing")
        fresh = md.get("quote_freshness_seconds")
        skew = md.get("cross_book_skew_warn_seconds")
        ok = isinstance(fresh, (int, float)) and isinstance(skew, (int, float))
        chk.add("quote freshness/skew config", PASS if ok else WARN,
                f"freshness={fresh}s skew_warn={skew}s" if ok
                else "freshness/skew thresholds not set")
    except Exception as e:  # noqa: BLE001
        chk.add("dual-book config", WARN, f"{type(e).__name__}")


def _check_store_readable(chk: Check) -> None:
    store = PROJECT_ROOT / "logs" / "official_resolutions.jsonl"
    if not store.exists():
        chk.add("official store readable", WARN, "absent (ok for a fresh run)")
        return
    try:
        import json as _json
        with open(store) as fh:
            line = fh.readline().strip()
        if line:
            _json.loads(line)
            chk.add("official store readable", PASS, "parses as JSONL")
        else:
            chk.add("official store readable", WARN, "empty")
    except (OSError, ValueError) as e:
        chk.add("official store readable", WARN, f"unreadable: {type(e).__name__}")


def _check_git_state(chk: Check, require_clean: bool) -> None:
    # Local-only (no fetch/pull/ls-remote) — same helper the runner uses to
    # compute run provenance, so preflight and the actual run agree.
    info = _rp.compute_git_info(PROJECT_ROOT)
    head = info["head"]
    if head == _rp.UNAVAILABLE:
        chk.add("git HEAD", WARN, "git unavailable or not a repository")
        chk.add("working tree clean", WARN, "could not determine (git unavailable)")
        return
    chk.add("git HEAD", PASS, f"{head[:12]} (branch {info['branch']})")
    dirty = info["dirty"]
    if dirty is True:
        msg = ("uncommitted changes present — a replication run must execute "
               "one fixed commit for its entire duration")
        if require_clean:
            chk.add("working tree clean", FAIL, msg + " (--require-clean-git)")
        else:
            chk.add("working tree clean", WARN, msg)
    else:
        chk.add("working tree clean", PASS, "no uncommitted changes")


def _check_config_hashes(chk: Check) -> None:
    strategy_hash = _rp.sha256_file(PROJECT_ROOT / "strategy.yaml")
    chk.add("strategy.yaml hash", PASS if strategy_hash else WARN,
            strategy_hash[:16] + "…" if strategy_hash else "strategy.yaml not found")
    req_hash = _rp.sha256_file(PROJECT_ROOT / "requirements.txt")
    chk.add("requirements.txt hash", PASS if req_hash else WARN,
            req_hash[:16] + "…" if req_hash else "requirements.txt not found")


def _check_no_competing_runner(chk: Check) -> None:
    try:
        out = subprocess.run(
            ["pgrep", "-af", "15m_bot_runner.py"],
            capture_output=True, text=True, timeout=5)
        # Exclude nothing special: pgrep only matches processes whose cmdline
        # contains the pattern, and this preflight script's own cmdline does
        # not, so no self-match is possible.
        lines = [l for l in out.stdout.splitlines() if l.strip()]
        if lines:
            pids = ", ".join(l.split()[0] for l in lines)
            chk.add("no competing runner", WARN,
                    f"15m_bot_runner.py already running (pid(s): {pids})")
        else:
            chk.add("no competing runner", PASS, "no other 15m_bot_runner.py process")
    except (OSError, subprocess.SubprocessError):
        chk.add("no competing runner", WARN, "pgrep unavailable")


def _check_collection_service_inactive(chk: Check) -> None:
    try:
        out = subprocess.run(
            ["systemctl", "--user", "list-units", "--all",
             "--no-legend", "--plain", "polymarket-collection-*.service"],
            capture_output=True, text=True, timeout=5)
        active = [l for l in out.stdout.splitlines() if " active " in f" {l} "]
        if active:
            chk.add("collection service not already active", WARN,
                    "; ".join(l.split()[0] for l in active))
        else:
            chk.add("collection service not already active", PASS,
                    "no polymarket-collection-*.service currently active")
    except (OSError, subprocess.SubprocessError):
        chk.add("collection service not already active", WARN, "systemctl unavailable")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="DRY-RUN preflight (read-only).")
    ap.add_argument("run_args", nargs="*",
                    help="the args you intend to pass to 15m_bot_runner.py")
    ap.add_argument("--require-clean-git", action="store_true",
                    help="fail (not just warn) if the working tree has "
                         "uncommitted changes — use before starting a "
                         "replication run that must execute one fixed "
                         "commit for its entire 7-day duration.")
    args = ap.parse_args(argv)

    chk = Check()
    _check_dry_run(chk)
    _check_no_live_flag(chk, args.run_args)
    _check_repo_and_venv(chk)
    _check_git_state(chk, args.require_clean_git)
    _check_config_hashes(chk)
    _check_no_competing_runner(chk)
    _check_collection_service_inactive(chk)
    _check_dirs(chk)
    _check_disk_space(chk)
    _check_data_files(chk)
    _check_store_readable(chk)
    _check_dualbook_config(chk)
    _check_dns(chk)
    _check_clock_sync(chk)
    _check_service(chk)
    _check_diag_signals(chk)

    print(chk.render())
    return 1 if chk.blocking_fail else 0


if __name__ == "__main__":
    sys.exit(main())
