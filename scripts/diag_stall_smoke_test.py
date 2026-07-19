#!/usr/bin/env python3
"""
Deterministic synthetic-stall smoke test — DIAGNOSTIC ONLY, NO NETWORK.

Proves the on-demand diagnostic capture path end to end WITHOUT touching the
Polymarket API or any real trading logic:

  1. Forks a child that installs the SIGUSR1 (native faulthandler all-thread
     traceback) and SIGUSR2 (rich JSON snapshot) handlers, records a couple of
     synthetic op timings, then deliberately WEDGES itself in a blocking loop
     (a stand-in for the real "heartbeat_stale" event-loop stall).
  2. The parent plays the role of the runner watchdog: it sends SIGUSR1 then
     SIGUSR2, waits, and verifies BOTH artifacts were produced —
     `faulthandler.log` (native stack) and a fresh `stall_diag_*.json`
     (rich redacted snapshot) — before killing the child.

Everything is deterministic and self-contained: a stalled synthetic process,
fixed dump directory, no sockets, no external services. Exit code 0 = PASS.

Run:  python scripts/diag_stall_smoke_test.py
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.diagnostics import runtime_diag as diag  # noqa: E402

CAPTURE_WAIT_S = 3.0


def _child(dump_dir: str) -> None:
    """Install diagnostics, log synthetic ops, then wedge (blocking loop)."""
    diag.install_diagnostics(dump_dir=dump_dir)
    # Synthetic op history a post-mortem would inspect (no network).
    diag.record_op("synthetic_fetch", 0.42, status="ok",
                   url="https://example.test/x?api_key=SHOULD_BE_REDACTED")
    diag.record_op("synthetic_wedge", 61.0, status="timeout",
                   error="synthetic stall for smoke test")
    # Deliberate stall: block forever. The parent will signal + kill us.
    while True:
        time.sleep(0.05)


def run_smoke_test(dump_dir: str) -> dict:
    base = Path(dump_dir)
    base.mkdir(parents=True, exist_ok=True)
    fault_log = base / "faulthandler.log"
    fault_before = fault_log.stat().st_size if fault_log.exists() else 0
    dumps_before = {p.name for p in base.glob("stall_diag_*.json")}

    pid = os.fork()
    if pid == 0:  # child
        try:
            _child(dump_dir)
        finally:
            os._exit(0)

    result = {"faulthandler_grew": False, "rich_dump": None, "pid": pid}
    try:
        time.sleep(1.0)  # let the child install handlers and start stalling
        # Play the watchdog: request native then rich dumps.
        os.kill(pid, signal.SIGUSR1)
        os.kill(pid, signal.SIGUSR2)
        time.sleep(CAPTURE_WAIT_S)

        if fault_log.exists() and fault_log.stat().st_size > fault_before:
            result["faulthandler_grew"] = True
        new_dumps = sorted(
            p for p in base.glob("stall_diag_*.json") if p.name not in dumps_before)
        if new_dumps:
            result["rich_dump"] = str(new_dumps[-1])
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
    return result


def main() -> int:
    dump_dir = str(Path(__file__).parent.parent / "logs" / "diagnostics")
    res = run_smoke_test(dump_dir)

    ok = res["faulthandler_grew"] and res["rich_dump"] is not None
    redaction_ok = True
    if res["rich_dump"]:
        payload = json.loads(Path(res["rich_dump"]).read_text())
        blob = json.dumps(payload)
        redaction_ok = "SHOULD_BE_REDACTED" not in blob
        ok = ok and redaction_ok

    print("=== synthetic-stall smoke test ===")
    print(f"  faulthandler (native SIGUSR1) captured : {res['faulthandler_grew']}")
    print(f"  rich JSON dump (SIGUSR2)               : {res['rich_dump']}")
    print(f"  secrets redacted in rich dump          : {redaction_ok}")
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
