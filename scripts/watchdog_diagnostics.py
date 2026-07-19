#!/usr/bin/env python3
"""
Watchdog diagnostics — READ-ONLY root-cause analysis (Phase 4).

Reads logs/runner_events.jsonl only. Does NOT modify the runner, does NOT
change the watchdog timeout, does NOT touch any strategy logic. It correlates
watchdog_restart events with the surrounding child lifecycle so a human can
judge WHY the heartbeat went stale.

Design constraints (per task):
- read-only; if the root cause is not proven from the logs, say so explicitly.
- do not redesign the runner or weaken/increase the watchdog timeout.

Definitions:
- STALE_THRESHOLD (runner) = 45 min = 2700 s (heartbeat) / 5400 s (journal).
- A watchdog_restart record carries the ages of heartbeat/journal/shadow files
  measured at restart time. If ALL three are stale together, the whole child
  event loop stalled (not just the heartbeat writer thread).

CLI:
  python3 scripts/watchdog_diagnostics.py [--since ISO/epoch] [--until ...]
                                          [--log-dir logs] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

RUNNER_EVENTS = "runner_events.jsonl"
HEARTBEAT_STALE_THRESHOLD_S = 45 * 60  # 2700 — matches runner (do not change here)
JOURNAL_STALE_THRESHOLD_S = 2 * 45 * 60  # 5400 — matches runner
# A restart is treated as a "full event-loop stall" (as opposed to only the
# heartbeat-writer thread dying) when journal AND shadow are also stale.
FULL_STALL_MARGIN_S = HEARTBEAT_STALE_THRESHOLD_S  # both > this => loop-wide


def _parse_when(value) -> float:
    """Accept epoch seconds or ISO-8601 (Z or offset) -> epoch float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def load_runner_events(log_dir: str):
    path = Path(log_dir) / RUNNER_EVENTS
    events = []
    if not path.exists():
        return events
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    events.sort(key=lambda e: e.get("ts", 0.0))
    return events


def _iso(ts):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _find_child_start(events, restart_ts, child_pid):
    """Most recent child_start for this pid at/before the restart."""
    best = None
    for e in events:
        if e.get("event_type") != "child_start":
            continue
        if e.get("ts", 0.0) > restart_ts:
            break
        # child_start does not carry pid match to restart's child_pid reliably;
        # use the newest child_start before the restart as the running child.
        best = e
    return best


def _find_child_exit(events, restart_ts, child_pid):
    """First child_exit for this pid at/after the restart."""
    for e in events:
        if e.get("event_type") != "child_exit":
            continue
        if e.get("ts", 0.0) < restart_ts:
            continue
        if e.get("child_pid") == child_pid:
            return e
    # fallback: first exit after restart regardless of pid
    for e in events:
        if e.get("event_type") == "child_exit" and e.get("ts", 0.0) >= restart_ts:
            return e
    return None


def analyze(log_dir: str = "logs", since=None, until=None) -> dict:
    events = load_runner_events(log_dir)
    since_ts = _parse_when(since)
    until_ts = _parse_when(until)

    def in_window(ts):
        if ts is None:
            return False
        if since_ts is not None and ts < since_ts:
            return False
        if until_ts is not None and ts > until_ts:
            return False
        return True

    counts = {}
    for e in events:
        et = e.get("event_type", "?")
        if since_ts is None and until_ts is None:
            counts[et] = counts.get(et, 0) + 1
        elif in_window(e.get("ts")):
            counts[et] = counts.get(et, 0) + 1

    restarts = []
    reason_counts = {}
    full_stall = 0
    heartbeat_only = 0

    for e in events:
        if e.get("event_type") != "watchdog_restart":
            continue
        ts = e.get("ts")
        if (since_ts is not None or until_ts is not None) and not in_window(ts):
            continue
        reason = e.get("reason")
        hb = e.get("heartbeat_age_s")
        jr = e.get("journal_age_s")
        sh = e.get("shadow_age_s")
        pid = e.get("child_pid")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

        journal_stale = jr is not None and jr > FULL_STALL_MARGIN_S
        shadow_stale = sh is not None and sh > FULL_STALL_MARGIN_S
        loop_wide = bool(journal_stale and shadow_stale)
        if reason == "heartbeat_stale":
            if loop_wide:
                full_stall += 1
            else:
                heartbeat_only += 1

        start = _find_child_start(events, ts, pid)
        exit_evt = _find_child_exit(events, ts, pid)
        uptime_before_stall = None
        if start is not None and start.get("ts") is not None:
            uptime_before_stall = round(ts - start["ts"], 1)

        restarts.append({
            "ts": ts,
            "t": _iso(ts),
            "reason": reason,
            "child_pid": pid,
            "heartbeat_age_s": hb,
            "journal_age_s": jr,
            "shadow_age_s": sh,
            "all_three_stale_together": loop_wide,
            "child_start_ts": start.get("ts") if start else None,
            "child_uptime_before_restart_s": uptime_before_stall,
            "exit_code": exit_evt.get("exit_code") if exit_evt else None,
            "exit_classification": exit_evt.get("classification") if exit_evt else None,
            "exit_watchdog_triggered": exit_evt.get("watchdog_triggered") if exit_evt else None,
        })

    # Clean self-restarts (planned ~90-min child self-restart, exit 0), for context.
    clean_exits = 0
    signal_exits = 0
    for e in events:
        if e.get("event_type") != "child_exit":
            continue
        if (since_ts is not None or until_ts is not None) and not in_window(e.get("ts")):
            continue
        code = e.get("exit_code")
        if code == 0:
            clean_exits += 1
        elif code is not None and code < 0:
            signal_exits += 1

    n_hb = reason_counts.get("heartbeat_stale", 0)
    conclusion = _conclude(n_hb, full_stall, heartbeat_only, restarts)

    return {
        "log_dir": log_dir,
        "window": {"since": _iso(since_ts), "until": _iso(until_ts)},
        "thresholds": {
            "heartbeat_stale_threshold_s": HEARTBEAT_STALE_THRESHOLD_S,
            "journal_stale_threshold_s": JOURNAL_STALE_THRESHOLD_S,
            "note": "read from runner; NOT changed by this diagnostic",
        },
        "event_counts": counts,
        "watchdog_restart_reason_counts": reason_counts,
        "heartbeat_stale_breakdown": {
            "total": n_hb,
            "all_three_files_stale_together": full_stall,
            "heartbeat_only": heartbeat_only,
        },
        "child_exit_context": {
            "clean_exit_code_0": clean_exits,
            "signal_terminated_exit": signal_exits,
        },
        "restarts": restarts,
        "root_cause": conclusion,
    }


def _conclude(n_hb, full_stall, heartbeat_only, restarts):
    if n_hb == 0:
        return {
            "proven": False,
            "summary": "No heartbeat_stale restarts in scope; nothing to attribute.",
        }
    ages = [r["heartbeat_age_s"] for r in restarts
            if r["reason"] == "heartbeat_stale" and r["heartbeat_age_s"] is not None]
    age_lo = min(ages) if ages else None
    age_hi = max(ages) if ages else None
    if full_stall > 0 and full_stall >= heartbeat_only:
        loc = (
            "In {full}/{n} heartbeat_stale restarts the heartbeat, journal AND "
            "shadow files were ALL stale together (each > {m}s). That points to a "
            "WHOLE event-loop stall in the child (a blocking call / hang in the "
            "main loop), not merely the heartbeat-writer thread dying. "
        ).format(full=full_stall, n=n_hb, m=FULL_STALL_MARGIN_S)
        proven = False  # logs show WHERE (loop-wide) but not the exact blocking call
        unproven = (
            "The exact blocking operation is NOT proven from runner_events alone "
            "(no per-call timing is logged). To pin it down, correlate the restart "
            "timestamps with the child stdout/stderr and any network/API timeouts. "
            "Do not change the watchdog timeout to mask this."
        )
    else:
        loc = (
            "Heartbeat went stale while journal/shadow were fresher in most cases, "
            "suggesting the heartbeat-writer path specifically lagged. "
        )
        proven = False
        unproven = (
            "Root cause NOT proven from runner_events alone; correlate with child "
            "logs around each restart timestamp."
        )
    return {
        "proven": proven,
        "heartbeat_age_range_s": [age_lo, age_hi],
        "summary": loc + unproven,
    }


def render_text(report: dict) -> str:
    L = []
    L.append("Watchdog Diagnostics (READ-ONLY root-cause analysis)")
    w = report["window"]
    L.append(f"- window: since={w['since']} until={w['until']}")
    th = report["thresholds"]
    L.append(f"- heartbeat stale threshold: {th['heartbeat_stale_threshold_s']}s "
             f"(journal {th['journal_stale_threshold_s']}s) — {th['note']}")
    L.append(f"- event counts: {report['event_counts']}")
    L.append(f"- watchdog_restart reasons: {report['watchdog_restart_reason_counts']}")
    hb = report["heartbeat_stale_breakdown"]
    L.append(f"- heartbeat_stale: {hb['total']} total | "
             f"{hb['all_three_files_stale_together']} loop-wide (hb+journal+shadow all stale) | "
             f"{hb['heartbeat_only']} heartbeat-only")
    ce = report["child_exit_context"]
    L.append(f"- child_exit context: {ce['clean_exit_code_0']} clean (exit 0) | "
             f"{ce['signal_terminated_exit']} signal-terminated")
    L.append("")
    L.append("Per-restart correlation:")
    for r in report["restarts"]:
        L.append(
            f"  {r['t']} reason={r['reason']} pid={r['child_pid']} "
            f"hb={r['heartbeat_age_s']}s journal={r['journal_age_s']}s shadow={r['shadow_age_s']}s "
            f"all3_stale={r['all_three_stale_together']} "
            f"uptime_before={r['child_uptime_before_restart_s']}s "
            f"exit_code={r['exit_code']} class={r['exit_classification']}"
        )
    rc = report["root_cause"]
    L.append("")
    L.append(f"Root cause proven: {rc['proven']}")
    if rc.get("heartbeat_age_range_s"):
        L.append(f"Heartbeat age at restart range (s): {rc['heartbeat_age_range_s']}")
    L.append(f"Summary: {rc['summary']}")
    return "\n".join(L)


def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only watchdog root-cause diagnostics.")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--log-dir", default="logs")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    report = analyze(log_dir=args.log_dir, since=args.since, until=args.until)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
