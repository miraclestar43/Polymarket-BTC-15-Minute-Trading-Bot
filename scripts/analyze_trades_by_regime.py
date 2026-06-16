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
    date_str = ""
    if len(sys.argv) > 1 and sys.argv[1] not in ("--all",):
        date_str = sys.argv[1]

    snapshots = load_regime_snapshots()
    orders = load_simulated_orders(date_str)
    joined = join_trades_to_regime(orders, snapshots)

    print(f"Loaded {len(snapshots)} regime snapshots")
    print(f"Loaded {len(orders)} actual simulated orders")
    matched = sum(1 for t in joined if t["join_gap_seconds"] is not None)
    print(f"Joined {matched}/{len(joined)} trades to a nearby regime snapshot "
          f"(<= {_MAX_JOIN_GAP_SECONDS // 60} min)")
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
