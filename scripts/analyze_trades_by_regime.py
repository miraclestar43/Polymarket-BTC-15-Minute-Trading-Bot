#!/usr/bin/env python3
"""
Analyze ACTUAL simulated trades by BTC regime (Phase 1).

Joins `simulated_order` records (from logs/trade_journal_*.jsonl) with the
NEAREST regime snapshot in logs/btc_regime_history.jsonl, then reports LONG vs
SHORT performance by:

  - macro_regime
  - intraday_regime
  - volatility_regime
  - combined macro+intraday regime

Design notes:
  - Uses ONLY actual simulated_order records and their realized
    `simulated_pnl`. It does NOT use shadow market resolution (which is
    unreliable) for actual-trade analysis.
  - If no regime snapshot is available near a trade, the trade is bucketed as
    "unavailable" rather than guessed.

Usage:
    python scripts/analyze_trades_by_regime.py            # all days
    python scripts/analyze_trades_by_regime.py 2026-06-12 # one day
    python scripts/analyze_trades_by_regime.py --all      # all days (explicit)
"""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Max seconds between a trade and a regime snapshot for the join to be trusted.
_MAX_JOIN_GAP_SECONDS = 30 * 60  # 30 minutes

# Per-bucket trade count below which results are flagged PRELIMINARY.
_SMALL_SAMPLE_THRESHOLD = 30


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def load_regime_snapshots(log_dir: str = "logs") -> List[Dict[str, Any]]:
    """Load combined regime snapshots written by the aggregator."""
    records: List[Dict[str, Any]] = []
    path = Path(log_dir) / "btc_regime_history.jsonl"
    if not path.exists():
        return records
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return sorted(records, key=lambda r: r.get("ts", 0))


def load_simulated_orders(date_str: str = "", log_dir: str = "logs") -> List[Dict[str, Any]]:
    """Load actual simulated_order records from the trade journal."""
    records: List[Dict[str, Any]] = []
    log_path = Path(log_dir)
    if date_str:
        files = [log_path / f"trade_journal_{date_str}.jsonl"]
    else:
        files = sorted(log_path.glob("trade_journal_*.jsonl"))
    for f in files:
        if f.exists():
            with open(f) as fh:
                for line in fh:
                    if line.strip():
                        try:
                            r = json.loads(line)
                            if r.get("event_type") == "simulated_order":
                                records.append(r)
                        except json.JSONDecodeError:
                            pass
    return records


# ---------------------------------------------------------------------------
# Run-window scoping (analyze ONE clean run, not mixed historical data)
# ---------------------------------------------------------------------------
def _parse_when(value: Optional[str]) -> Optional[float]:
    """Parse a --since/--until value (epoch seconds or ISO-8601) to a ts."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def latest_run_window(log_dir: str = "logs") -> Optional[Dict[str, Any]]:
    """Infer the most recent run's [start_ts, stop_ts] from runner_events.jsonl."""
    path = Path(log_dir) / "runner_events.jsonl"
    if not path.exists():
        return None
    events: List[Dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    starts = [e for e in events if e.get("event_type") == "runner_start" and e.get("ts")]
    if not starts:
        return None
    start = max(starts, key=lambda e: e.get("ts", 0))
    start_ts = start.get("ts")
    stops = [
        e for e in events
        if e.get("event_type") == "runner_stop" and (e.get("ts") or 0) >= start_ts
    ]
    stop = min(stops, key=lambda e: e.get("ts", 0)) if stops else None
    return {
        "start_ts": start_ts,
        "stop_ts": stop.get("ts") if stop else None,
        "start_t": start.get("t"),
        "stop_t": stop.get("t") if stop else None,
    }


def filter_orders_window(
    orders: List[Dict[str, Any]],
    since: Optional[float],
    until: Optional[float],
) -> List[Dict[str, Any]]:
    """Keep only orders whose timestamp falls within [since, until]."""
    if since is None and until is None:
        return orders
    out: List[Dict[str, Any]] = []
    for o in orders:
        t = _to_ts(o.get("timestamp", ""))
        if t is None:
            continue
        if since is not None and t < since:
            continue
        if until is not None and t > until:
            continue
        out.append(o)
    return out


# ---------------------------------------------------------------------------
# Join
# ---------------------------------------------------------------------------
def _to_ts(ts_str: str) -> Optional[float]:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


def find_nearest_snapshot(
    trade_ts: float,
    snapshots: List[Dict[str, Any]],
    max_gap: float = _MAX_JOIN_GAP_SECONDS,
) -> Dict[str, Any]:
    """
    Find the regime snapshot closest in time to `trade_ts`. Returns an
    all-"unavailable" record if none is within `max_gap`.
    """
    unavailable = {
        "macro_regime": "unavailable",
        "intraday_regime": "unavailable",
        "volatility_regime": "unavailable",
        "combined_regime": "unavailable|unavailable",
        "join_gap_seconds": None,
    }
    if not snapshots:
        return unavailable

    best = None
    best_diff = float("inf")
    for rec in snapshots:
        diff = abs(rec.get("ts", 0) - trade_ts)
        if diff < best_diff:
            best_diff = diff
            best = rec

    if best is None or best_diff > max_gap:
        return unavailable

    return {
        "macro_regime": best.get("macro_regime", "unavailable"),
        "intraday_regime": best.get("intraday_regime", "unavailable"),
        "volatility_regime": best.get("volatility_regime", "unavailable"),
        "combined_regime": best.get(
            "combined_regime",
            f"{best.get('macro_regime', 'unavailable')}|{best.get('intraday_regime', 'unavailable')}",
        ),
        "join_gap_seconds": round(best_diff, 1),
    }


def join_trades_to_regime(
    orders: List[Dict[str, Any]],
    snapshots: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach nearest regime fields to each actual simulated order."""
    joined: List[Dict[str, Any]] = []
    for order in orders:
        trade_ts = _to_ts(order.get("timestamp", ""))
        if trade_ts is None:
            continue
        regime = find_nearest_snapshot(trade_ts, snapshots)
        joined.append({
            "timestamp": order.get("timestamp"),
            "side": order.get("side", "unknown"),
            "entry_price": order.get("entry_price"),
            "pnl": order.get("simulated_pnl", 0) or 0,
            "p_model": order.get("p_model"),
            "macro_regime": regime["macro_regime"],
            "intraday_regime": regime["intraday_regime"],
            "volatility_regime": regime["volatility_regime"],
            "combined_regime": regime["combined_regime"],
            "join_gap_seconds": regime["join_gap_seconds"],
        })
    return joined


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _summarize(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    wins = sum(1 for t in trades if t["pnl"] > 0)
    pnl = sum(t["pnl"] for t in trades)
    n = len(trades)
    return {
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": (wins / n * 100) if n else 0.0,
        "total_pnl": pnl,
        "avg_pnl": (pnl / n) if n else 0.0,
    }


def report_by_dimension(joined: List[Dict[str, Any]], dimension: str) -> Dict[str, Dict[str, Any]]:
    """Group LONG/SHORT summaries by a regime dimension key."""
    buckets: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(
        lambda: {"long": [], "short": []}
    )
    for t in joined:
        side = t["side"]
        if side not in ("long", "short"):
            continue
        buckets[t[dimension]][side].append(t)

    out: Dict[str, Dict[str, Any]] = {}
    for regime, sides in buckets.items():
        out[regime] = {
            "long": _summarize(sides["long"]),
            "short": _summarize(sides["short"]),
        }
    return out


def _print_dimension(title: str, report: Dict[str, Dict[str, Any]]) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")
    if not report:
        print("  (no trades)")
        return
    for regime in sorted(report.keys()):
        print(f"\n--- {regime} ---")
        for side in ("long", "short"):
            s = report[regime][side]
            if s["trades"] == 0:
                print(f"  {side.upper():5}: 0 trades")
                continue
            prelim = "  [PRELIMINARY n<%d]" % _SMALL_SAMPLE_THRESHOLD if s["trades"] < _SMALL_SAMPLE_THRESHOLD else ""
            print(
                f"  {side.upper():5}: {s['trades']} trades, "
                f"{s['wins']}W/{s['losses']}L, WR={s['win_rate']:.0f}%, "
                f"P/L=${s['total_pnl']:+.4f}, avg=${s['avg_pnl']:+.4f}{prelim}"
            )


def main() -> None:
    def _arg_value(flag: str) -> Optional[str]:
        for i, a in enumerate(sys.argv):
            if a == flag and i + 1 < len(sys.argv):
                return sys.argv[i + 1]
            if a.startswith(flag + "="):
                return a.split("=", 1)[1]
        return None

    flags = {"--all", "--latest-run", "--json"}
    since = _parse_when(_arg_value("--since"))
    until = _parse_when(_arg_value("--until"))
    scope_label = None

    if "--latest-run" in sys.argv:
        win = latest_run_window()
        if win is None:
            print("No runner_start events found — cannot infer the latest run.")
            return
        since = win["start_ts"]
        until = win["stop_ts"]
        scope_label = f"latest run ({win['start_t']} -> {win['stop_t'] or 'OPEN'})"

    # Optional positional date (backwards compatible), ignoring flags/values.
    date_str = ""
    skip_next = False
    for a in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if a in ("--since", "--until"):
            skip_next = True
            continue
        if a in flags or a.startswith("--since=") or a.startswith("--until="):
            continue
        date_str = a
        break

    snapshots = load_regime_snapshots()
    orders = load_simulated_orders(date_str)
    before = len(orders)
    orders = filter_orders_window(orders, since, until)
    joined = join_trades_to_regime(orders, snapshots)

    if scope_label:
        print(f"SCOPE: {scope_label}")
    elif since is not None or until is not None:
        print(f"SCOPE: custom window since={since} until={until}")
    else:
        print("SCOPE: ALL HISTORY (not scoped to one run — use --latest-run)")
    print(f"Loaded {len(snapshots)} regime snapshots")
    print(f"Loaded {len(orders)} actual simulated orders"
          + (f" (filtered from {before} across all journals)" if (since is not None or until is not None) else ""))
    matched = sum(1 for t in joined if t["join_gap_seconds"] is not None)
    print(f"Joined {matched}/{len(joined)} trades to a nearby regime snapshot "
          f"(<= {_MAX_JOIN_GAP_SECONDS // 60} min); "
          f"{len(joined) - matched} unmatched (no snapshot within window -> 'unavailable')")
    print("NOTE: actual-trade analysis uses realized simulated_pnl only; "
          "shadow market resolution is NOT used.")
    if len(orders) < _SMALL_SAMPLE_THRESHOLD:
        print(f"*** PRELIMINARY: only {len(orders)} actual trades "
              f"(< {_SMALL_SAMPLE_THRESHOLD}); per-regime results are NOT "
              "statistically reliable. ***")

    _print_dimension("BY MACRO REGIME", report_by_dimension(joined, "macro_regime"))
    _print_dimension("BY INTRADAY REGIME", report_by_dimension(joined, "intraday_regime"))
    _print_dimension("BY VOLATILITY REGIME", report_by_dimension(joined, "volatility_regime"))
    _print_dimension("BY COMBINED (MACRO|INTRADAY) REGIME",
                     report_by_dimension(joined, "combined_regime"))
    print()


if __name__ == "__main__":
    main()
