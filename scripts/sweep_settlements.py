#!/usr/bin/env python3
"""
Idempotent settlement sweeper — DRY-RUN / DIAGNOSTIC ONLY.

Scans recorded shadow/baseline `would_trade` decisions, derives each market's
stable identity + 15-min window, and settles markets whose window has finished
using the offline source hierarchy (official cache -> BTC inference ->
unavailable). Results are written to a DERIVED settlement dataset
(logs/settlements.jsonl) atomically. The original decision logs are never
mutated.

Idempotent & resumable:
  * one settlement per stable market key (condition id / slug),
  * already-`resolved` markets are skipped unless --force,
  * `open` / `unavailable` markets are retried on the next sweep,
  * the dataset is rewritten atomically (tmp file + os.replace), so an
    interrupted run never corrupts prior results.

Offline by default: uses only logs/market_resolutions_cache.json and
logs/btc_price_history.jsonl. No network, no orders, no secrets.

Usage:
    python scripts/sweep_settlements.py --latest-run
    python scripts/sweep_settlements.py --since ISO --until ISO
    python scripts/sweep_settlements.py --since 2026-06-26T00:43:30Z \
        --until 2026-07-10T09:58:00Z          # backfill completed run
    python scripts/sweep_settlements.py --all --force
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.strategy_brain.settlement import (  # noqa: E402
    load_btc_price_points,
    load_resolution_cache,
    load_official_resolutions,
    market_identity,
    settle_market,
)
from core.strategy_brain.market_resolver import (  # noqa: E402
    OUTCOME_OFFICIAL,
    OUTCOME_INFERRED,
    OUTCOME_UNAVAILABLE,
)
# Reuse the single implementation of run-window scoping + record loading.
from analyze_strategy_experiments import (  # noqa: E402
    latest_run_window,
    load_shadow_records,
    _parse_when,
)

_SETTLEMENTS_FILE = "settlements.jsonl"


def load_settlements(log_dir: str = "logs") -> Dict[str, Dict[str, Any]]:
    """Load existing settlements keyed by stable_key (last write wins)."""
    path = Path(log_dir) / _SETTLEMENTS_FILE
    out: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = rec.get("stable_key")
            if key:
                out[key] = rec
    return out


def write_settlements_atomic(
    settlements: Dict[str, Dict[str, Any]], log_dir: str = "logs"
) -> Path:
    """Rewrite the whole settlement dataset atomically (tmp + os.replace)."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    final = log_path / _SETTLEMENTS_FILE
    tmp = log_path / f"{_SETTLEMENTS_FILE}.tmp"
    with open(tmp, "w") as f:
        for key in sorted(settlements):
            f.write(json.dumps(settlements[key]) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    return final


def unique_markets(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Collect unique market identities from would_trade decisions."""
    markets: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        if not rec.get("would_trade"):
            continue
        ident = market_identity(rec)
        key = ident.get("stable_key")
        if key and key != "unknown" and key not in markets:
            markets[key] = ident
    return markets


def sweep(
    log_dir: str = "logs",
    since: Optional[float] = None,
    until: Optional[float] = None,
    official_only: bool = False,
    force: bool = False,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Run one settlement sweep. Returns a summary dict."""
    records = load_shadow_records("", log_dir=log_dir, since=since, until=until)
    markets = unique_markets(records)

    existing = load_settlements(log_dir)
    resolution_cache = load_resolution_cache(log_dir)
    official_store = load_official_resolutions(log_dir)
    price_points = load_btc_price_points(log_dir)
    if now_ts is None:
        now_ts = datetime.now(timezone.utc).timestamp()

    processed = 0
    skipped_resolved = 0
    source_counts: Counter = Counter()
    status_counts: Counter = Counter()

    for key, ident in markets.items():
        prior = existing.get(key)
        prior_official = bool(
            prior and prior.get("resolution_source") == OUTCOME_OFFICIAL)
        if (prior and prior.get("resolution_status") == "resolved" and not force
                and (prior_official or not official_store)):
            # Keep official results; keep inferred results only when there is no
            # official store that might upgrade them. Otherwise fall through and
            # re-settle so an inferred outcome can be upgraded to official.
            skipped_resolved += 1
            source_counts[prior.get("resolution_source", OUTCOME_UNAVAILABLE)] += 1
            status_counts[prior.get("resolution_status")] += 1
            continue
        settlement = settle_market(
            ident,
            resolution_cache=resolution_cache,
            official_store=official_store,
            price_points=price_points,
            now_ts=now_ts,
            official_only=official_only,
        )
        existing[key] = settlement
        processed += 1
        source_counts[settlement.get("resolution_source", OUTCOME_UNAVAILABLE)] += 1
        status_counts[settlement.get("resolution_status")] += 1

    path = write_settlements_atomic(existing, log_dir)

    resolved = sum(
        1 for s in existing.values() if s.get("resolution_status") == "resolved"
    )
    official = sum(
        1 for s in existing.values()
        if s.get("resolution_source") == OUTCOME_OFFICIAL
    )
    inferred = sum(
        1 for s in existing.values()
        if s.get("resolution_source") == OUTCOME_INFERRED
    )
    return {
        "dataset": str(path),
        "markets_in_scope": len(markets),
        "processed": processed,
        "skipped_already_resolved": skipped_resolved,
        "total_settlements": len(existing),
        "resolved": resolved,
        "official_resolution": official,
        "inferred_from_market_data": inferred,
        "unavailable": len(existing) - resolved,
        "sweep_source_counts": dict(source_counts),
        "sweep_status_counts": dict(status_counts),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Idempotent settlement sweeper (dry-run/diagnostic)")
    ap.add_argument("--since", help="ISO ts or epoch (inclusive)")
    ap.add_argument("--until", help="ISO ts or epoch (inclusive)")
    ap.add_argument("--latest-run", action="store_true", help="scope to latest run window")
    ap.add_argument("--all", action="store_true", help="all history (no scope)")
    ap.add_argument("--official-only", action="store_true",
                    help="only official resolutions; never infer from BTC")
    ap.add_argument("--force", action="store_true", help="re-settle already-resolved markets")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--json", action="store_true", help="print summary as JSON")
    args = ap.parse_args()

    since = _parse_when(args.since)
    until = _parse_when(args.until)
    label = None
    if args.latest_run:
        win = latest_run_window(args.log_dir)
        if win is None:
            print("No runner_start events found — cannot infer the latest run.")
            return
        since, until = win["start_ts"], win["stop_ts"]
        label = f"latest run ({win['start_t']} -> {win['stop_t'] or 'OPEN'})"

    summary = sweep(
        log_dir=args.log_dir,
        since=since,
        until=until,
        official_only=args.official_only,
        force=args.force,
    )
    summary["scope"] = label or f"since={since} until={until}"

    if args.json:
        print(json.dumps(summary, indent=2))
        return
    print("=" * 70)
    print("SETTLEMENT SWEEP (dry-run / diagnostic — no orders, offline)")
    print("=" * 70)
    print(f"scope: {summary['scope']}")
    print(f"dataset: {summary['dataset']}")
    print(f"markets in scope: {summary['markets_in_scope']}")
    print(f"processed this sweep: {summary['processed']}  "
          f"skipped (already resolved): {summary['skipped_already_resolved']}")
    print(f"total settlements: {summary['total_settlements']}  resolved: {summary['resolved']}")
    print(f"  official_resolution: {summary['official_resolution']}")
    print(f"  inferred_from_market_data: {summary['inferred_from_market_data']}")
    print(f"  unavailable: {summary['unavailable']}")
    print(f"sweep status counts: {summary['sweep_status_counts']}")


if __name__ == "__main__":
    main()
