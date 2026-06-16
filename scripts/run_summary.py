#!/usr/bin/env python3
"""
Reliable Run Summary (observability, diagnostics only)

Produces a trustworthy summary of a dry-run from STRUCTURED sources:

  - logs/runner_events.jsonl   : lifecycle events (source of truth for restarts)
  - logs/trade_journal_*.jsonl : actual simulated_order records (realized PnL)
  - logs/btc_regime_history.jsonl : regime snapshots
  - logs/btc_price_history.jsonl  : spot price history (coverage span)

Goals / fixes vs. the old grep-based reporting:
  - Restart counts are grouped BY CAUSE and never double-counted
    (a watchdog restart is NOT also counted as a "normal" restart).
  - Wall-clock span uses runner_start -> last event; cumulative child uptime is
    summed from child_exit events (fixes the 47h-vs-70h discrepancy).
  - Watchdog restarts are surfaced as WARNINGS.
  - Small actual-trade samples are labelled PRELIMINARY.

This script NEVER affects trading. It only reads logs. It does not use shadow
market resolution for actual-trade analysis.

Usage:
    python scripts/run_summary.py             # all available logs
    python scripts/run_summary.py --json      # machine-readable JSON
"""
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Below this many actual simulated trades, conclusions are PRELIMINARY.
SMALL_SAMPLE_THRESHOLD = 30

# Exit classifications that are considered planned/benign restarts.
_BENIGN = {"planned_restart", "clean_exit"}
_WARN = {"watchdog_restart", "external_sigterm"}
_ERROR = {"crash_error"}


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.exists():
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def load_runner_events(log_dir: str = "logs") -> List[Dict[str, Any]]:
    return _read_jsonl(Path(log_dir) / "runner_events.jsonl")


def load_simulated_orders(log_dir: str = "logs") -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for f in sorted(Path(log_dir).glob("trade_journal_*.jsonl")):
        for r in _read_jsonl(f):
            if r.get("event_type") == "simulated_order":
                records.append(r)
    return records


def load_regime_snapshots(log_dir: str = "logs") -> List[Dict[str, Any]]:
    return _read_jsonl(Path(log_dir) / "btc_regime_history.jsonl")


def load_price_history(log_dir: str = "logs") -> List[Dict[str, Any]]:
    return _read_jsonl(Path(log_dir) / "btc_price_history.jsonl")


def _pid_alive(pid: Any) -> bool:
    """Best-effort check whether a PID is currently running. Never raises."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except (OSError, TypeError, ValueError):
        return False
    return True


# ---------------------------------------------------------------------------
# Aggregation (pure functions — testable)
# ---------------------------------------------------------------------------
def summarize_runner_events(
    events: List[Dict[str, Any]],
    close_ts: Optional[float] = None,
    active: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Build a reliable runtime + restart summary from structured runner events.

    - wall_clock_span_s: runner_start -> runner_stop when completed; otherwise
      runner_start (first event) -> effective_end (current time / latest log ts).
    - cumulative_child_uptime_s: sum of child_exit uptimes, plus a still-open
      child estimated from its child_start to effective_end.
    - restart_by_cause: Counter of child_exit classifications (no double count).
    - watchdog_restart_events: count of explicit watchdog_restart events.
    - run_status: completed / active / abruptly_stopped /
      active_or_abruptly_stopped / unknown / no_events.

    Args:
      close_ts: timestamp used to close any open interval (open child / open
        run). Pass current time for an active run, or the latest known log
        timestamp if the process is no longer running. Never moves the end
        before the last recorded event.
      active: True/False if liveness is known (drives run_status); None if
        unknown (yields "active_or_abruptly_stopped").
    """
    result: Dict[str, Any] = {
        "wall_clock_span_s": 0.0,
        "cumulative_child_uptime_s": 0.0,
        "child_starts": 0,
        "child_exits": 0,
        "restart_by_cause": {},
        "watchdog_restart_events": 0,
        "runner_loop_errors": 0,
        "first_event_t": None,
        "last_event_t": None,
        "open_child": False,
        "open_runner": False,
        "run_status": "no_events",
    }
    if not events:
        return result

    ev = sorted(events, key=lambda e: e.get("ts", 0))
    first_ts = ev[0].get("ts", 0)
    last_ts = ev[-1].get("ts", 0)
    result["first_event_t"] = ev[0].get("t")
    result["last_event_t"] = ev[-1].get("t")

    has_runner_start = any(e.get("event_type") == "runner_start" for e in ev)
    has_runner_stop = any(e.get("event_type") == "runner_stop" for e in ev)

    # Timestamp used to close still-open intervals. Never goes backwards before
    # the last recorded event.
    effective_end = last_ts
    if close_ts is not None:
        effective_end = max(effective_end, close_ts)

    causes: Counter = Counter()
    uptime = 0.0
    starts = 0
    exits = 0
    open_start_ts: Optional[float] = None

    for e in ev:
        et = e.get("event_type")
        if et == "child_start":
            starts += 1
            open_start_ts = e.get("ts")
        elif et == "child_exit":
            exits += 1
            up = e.get("child_uptime_s")
            if isinstance(up, (int, float)):
                uptime += up
            causes[e.get("classification", "unknown")] += 1
            open_start_ts = None
        elif et == "watchdog_restart":
            result["watchdog_restart_events"] += 1
        elif et == "runner_loop_error":
            result["runner_loop_errors"] += 1

    # Open child interval: a child_start with no matching child_exit. Estimate
    # its uptime up to effective_end (current time if active, else latest log).
    if open_start_ts is not None:
        uptime += max(0.0, effective_end - open_start_ts)
        result["open_child"] = True

    # Wall-clock span: avoid reporting 0h00m00s when only runner_start /
    # child_start were recorded before an abrupt kill. When there is no
    # runner_stop, extend the span to effective_end (latest known timestamp).
    if has_runner_stop:
        result["wall_clock_span_s"] = round(max(0.0, last_ts - first_ts), 1)
    else:
        result["wall_clock_span_s"] = round(max(0.0, effective_end - first_ts), 1)

    result["open_runner"] = has_runner_start and not has_runner_stop
    if has_runner_stop:
        result["run_status"] = "completed"
    elif has_runner_start:
        if active is True:
            result["run_status"] = "active"
        elif active is False:
            result["run_status"] = "abruptly_stopped"
        else:
            result["run_status"] = "active_or_abruptly_stopped"
    else:
        result["run_status"] = "unknown"

    result["cumulative_child_uptime_s"] = round(uptime, 1)
    result["child_starts"] = starts
    result["child_exits"] = exits
    result["restart_by_cause"] = dict(causes)
    return result


def summarize_trades(orders: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Actual simulated-order summary using realized simulated_pnl only."""
    n = len(orders)
    wins = sum(1 for o in orders if (o.get("simulated_pnl") or 0) > 0)
    pnl = sum((o.get("simulated_pnl") or 0) for o in orders)
    by_side: Dict[str, Dict[str, float]] = {}
    for o in orders:
        side = o.get("side", "unknown")
        d = by_side.setdefault(side, {"trades": 0, "wins": 0, "pnl": 0.0})
        d["trades"] += 1
        if (o.get("simulated_pnl") or 0) > 0:
            d["wins"] += 1
        d["pnl"] += (o.get("simulated_pnl") or 0)
    return {
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate_pct": round(wins / n * 100, 1) if n else 0.0,
        "total_pnl": round(pnl, 4),
        "by_side": {
            s: {
                "trades": d["trades"],
                "wins": d["wins"],
                "win_rate_pct": round(d["wins"] / d["trades"] * 100, 1) if d["trades"] else 0.0,
                "total_pnl": round(d["pnl"], 4),
            }
            for s, d in by_side.items()
        },
        "preliminary": n < SMALL_SAMPLE_THRESHOLD,
        "small_sample_threshold": SMALL_SAMPLE_THRESHOLD,
    }


def _span_seconds(records: List[Dict[str, Any]]) -> float:
    ts = [r.get("ts", 0) for r in records if r.get("ts")]
    return round(max(ts) - min(ts), 1) if len(ts) >= 2 else 0.0


def build_summary(log_dir: str = "logs", now_ts: Optional[float] = None) -> Dict[str, Any]:
    events = load_runner_events(log_dir)
    orders = load_simulated_orders(log_dir)
    regimes = load_regime_snapshots(log_dir)
    prices = load_price_history(log_dir)

    # Determine whether the runner is still alive and the latest trustworthy
    # timestamp to close an open interval. An open run (runner_start without
    # runner_stop) is estimated to current time if active, else to the latest
    # known log timestamp across all sources.
    runner_pid: Any = None
    has_stop = False
    for e in events:
        et = e.get("event_type")
        if et == "runner_start":
            runner_pid = e.get("pid", runner_pid)
        elif et == "runner_stop":
            has_stop = True

    active_known = bool(runner_pid) and not has_stop
    active = _pid_alive(runner_pid) if active_known else False

    all_ts: List[float] = [e.get("ts") for e in events if e.get("ts")]
    all_ts += [r.get("ts") for r in regimes if r.get("ts")]
    all_ts += [p.get("ts") for p in prices if p.get("ts")]
    latest_log_ts = max(all_ts) if all_ts else None

    now = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
    close_ts = now if active else latest_log_ts

    runner = summarize_runner_events(
        events, close_ts=close_ts, active=active if active_known else None
    )
    trades = summarize_trades(orders)

    regime_counts = Counter(r.get("combined_regime", "unavailable") for r in regimes)

    warnings: List[str] = []
    if runner["run_status"] in ("abruptly_stopped", "active_or_abruptly_stopped"):
        warnings.append(
            "runner_stop not recorded — run is active or was stopped abruptly; "
            "uptime/span estimated to the latest known timestamp"
        )
    if runner["watchdog_restart_events"] > 0:
        warnings.append(
            f"{runner['watchdog_restart_events']} watchdog restart(s) — investigate child stalls"
        )
    if runner["restart_by_cause"].get("external_sigterm", 0) > 0:
        warnings.append(
            f"{runner['restart_by_cause']['external_sigterm']} external SIGTERM(s) — "
            "another supervisor may be killing the bot"
        )
    if runner["restart_by_cause"].get("crash_error", 0) > 0:
        warnings.append(
            f"{runner['restart_by_cause']['crash_error']} crash/error exit(s)"
        )
    if not events:
        warnings.append("no runner_events.jsonl found — start the runner to record lifecycle events")
    if trades["preliminary"]:
        warnings.append(
            f"PRELIMINARY: only {trades['trades']} actual trades "
            f"(< {SMALL_SAMPLE_THRESHOLD}); conclusions are not statistically reliable"
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runner": runner,
        "trades": trades,
        "regime_snapshots": {
            "count": len(regimes),
            "span_s": _span_seconds(regimes),
            "combined_regime_distribution": dict(regime_counts.most_common()),
        },
        "price_history": {
            "count": len(prices),
            "span_s": _span_seconds(prices),
        },
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _fmt_hms(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def print_summary(summary: Dict[str, Any]) -> None:
    r = summary["runner"]
    t = summary["trades"]

    print("=" * 70)
    print("RUN SUMMARY (reliable, structured)")
    print("=" * 70)
    print(f"Generated:            {summary['generated_at']}")
    print(f"RUN STATUS:           {r['run_status']}")
    print(f"First event:          {r['first_event_t']}")
    print(f"Last event:           {r['last_event_t']}")
    print(f"Wall-clock span:      {_fmt_hms(r['wall_clock_span_s'])}"
          + ("  (estimated — runner_stop not recorded)" if r["open_runner"] else ""))
    print(f"Cumulative uptime:    {_fmt_hms(r['cumulative_child_uptime_s'])}"
          + (" (a child may still be running)" if r["open_child"] else ""))
    print(f"Child starts/exits:   {r['child_starts']} / {r['child_exits']}")

    print("\n--- Restarts by cause (deduplicated) ---")
    if r["restart_by_cause"]:
        for cause, n in sorted(r["restart_by_cause"].items()):
            tag = "WARN" if cause in _WARN else ("ERROR" if cause in _ERROR else "ok")
            print(f"  [{tag:5}] {cause:18} {n}")
    else:
        print("  (no child_exit events recorded)")
    print(f"  watchdog_restart events (explicit): {r['watchdog_restart_events']}")
    print(f"  runner loop errors:                 {r['runner_loop_errors']}")

    print("\n--- Actual simulated trades (realized PnL, no shadow resolution) ---")
    label = "  *** PRELIMINARY — SMALL SAMPLE ***" if t["preliminary"] else ""
    print(f"  Trades: {t['trades']}  W/L: {t['wins']}/{t['losses']}  "
          f"WR: {t['win_rate_pct']}%  PnL: ${t['total_pnl']:+.4f}{label}")
    for side, d in sorted(t["by_side"].items()):
        print(f"    {side:6}: {d['trades']} trades, WR={d['win_rate_pct']}%, "
              f"PnL=${d['total_pnl']:+.4f}")

    rs = summary["regime_snapshots"]
    print("\n--- Regime snapshots ---")
    print(f"  Count: {rs['count']}  Span: {_fmt_hms(rs['span_s'])}")
    for regime, n in rs["combined_regime_distribution"].items():
        print(f"    {regime:28} {n}")

    ph = summary["price_history"]
    print(f"\n--- BTC price history ---\n  Points: {ph['count']}  Span: {_fmt_hms(ph['span_s'])}")

    print("\n--- Warnings ---")
    if summary["warnings"]:
        for w in summary["warnings"]:
            print(f"  ! {w}")
    else:
        print("  (none)")
    print()


def main() -> None:
    as_json = "--json" in sys.argv
    summary = build_summary()
    if as_json:
        print(json.dumps(summary, indent=2))
    else:
        print_summary(summary)


if __name__ == "__main__":
    main()
