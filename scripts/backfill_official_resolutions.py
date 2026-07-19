#!/usr/bin/env python3
"""
Official-resolution backfill (CLOB) — DRY-RUN / DIAGNOSTIC ONLY.

Retrieves REAL Polymarket outcomes for finished 15-min BTC markets from the
CLOB endpoint (the Gamma path was broken — see core/strategy_brain/
official_resolution.py) and writes them to a clean, condition-id-validated
store: logs/official_resolutions.jsonl. The settlement sweeper then prefers
these official outcomes over BTC inference.

Read-only with respect to the exchange (HTTP GET only). NEVER places, signs, or
submits an order. NEVER writes to the strategy or its logs; only the derived
official store. Idempotent, resumable, rate-limited.

Idempotent:
  * markets already `resolved` in the store are skipped unless --force,
  * `pending` / `not_found` / `network_failure` / `malformed` are retried,
  * the store is rewritten atomically (tmp + os.replace).

Offline fixture mode (no network) for tests / reproducibility:
    --fixtures path/to/fixtures.json   # {condition_id: clob_payload_or_marker}

Usage:
    python scripts/backfill_official_resolutions.py --since ISO --until ISO
    python scripts/backfill_official_resolutions.py --latest-run --limit 50
    python scripts/backfill_official_resolutions.py --all --force --sleep 0.25
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.strategy_brain.settlement import extract_condition_id, market_identity  # noqa: E402
from core.strategy_brain.official_resolution import (  # noqa: E402
    OFFICIAL_STORE_FILE,
    STATUS_RESOLVED,
    fetch_official,
    make_fixture_opener,
)
sys.path.insert(0, str(Path(__file__).parent))
from analyze_strategy_experiments import (  # noqa: E402
    latest_run_window,
    load_shadow_records,
    _parse_when,
)


def load_store(log_dir: str) -> Dict[str, Dict[str, Any]]:
    path = Path(log_dir) / OFFICIAL_STORE_FILE
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
            cid = rec.get("requested_condition_id") or rec.get("condition_id")
            if cid:
                out[str(cid).lower()] = rec
    return out


def write_store_atomic(store: Dict[str, Dict[str, Any]], log_dir: str) -> Path:
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    final = log_path / OFFICIAL_STORE_FILE
    tmp = log_path / f"{OFFICIAL_STORE_FILE}.tmp"
    with open(tmp, "w") as f:
        for key in sorted(store):
            f.write(json.dumps(store[key]) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, final)
    return final


def unique_condition_ids(records: List[Dict[str, Any]]) -> List[str]:
    seen: Dict[str, None] = {}
    for rec in records:
        if not rec.get("would_trade"):
            continue
        cid = extract_condition_id(rec.get("market_id"))
        if not cid:
            # fall back to identity (may still yield a condition id)
            cid = market_identity(rec).get("condition_id")
        if cid:
            seen.setdefault(cid, None)
    return list(seen)


def backfill(
    log_dir: str = "logs",
    since: Optional[float] = None,
    until: Optional[float] = None,
    force: bool = False,
    limit: Optional[int] = None,
    sleep_s: float = 0.2,
    fixtures: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    records = load_shadow_records("", log_dir=log_dir, since=since, until=until)
    cids = unique_condition_ids(records)
    store = load_store(log_dir)
    opener = make_fixture_opener(fixtures) if fixtures is not None else None

    status_counts: Counter = Counter()
    attempted = 0
    skipped = 0
    for cid in cids:
        prior = store.get(cid.lower())
        if prior and prior.get("status") == STATUS_RESOLVED and not force:
            skipped += 1
            status_counts[STATUS_RESOLVED] += 1
            continue
        if limit is not None and attempted >= limit:
            break
        rec = fetch_official(cid, opener=opener)
        store[cid.lower()] = rec
        status_counts[rec.get("status", "unknown")] += 1
        attempted += 1
        if opener is None and sleep_s > 0:
            time.sleep(sleep_s)  # polite pacing for live network backfill

    path = write_store_atomic(store, log_dir)
    resolved = sum(1 for r in store.values() if r.get("status") == STATUS_RESOLVED)
    return {
        "store": str(path),
        "condition_ids_in_scope": len(cids),
        "attempted": attempted,
        "skipped_already_resolved": skipped,
        "total_in_store": len(store),
        "resolved_in_store": resolved,
        "sweep_status_counts": dict(status_counts),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CLOB official-resolution backfill (diagnostic).")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--latest-run", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="max markets to fetch this run")
    ap.add_argument("--sleep", type=float, default=0.2, help="seconds between live requests")
    ap.add_argument("--fixtures", help="path to offline fixtures JSON (no network)")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    since = _parse_when(args.since)
    until = _parse_when(args.until)
    if args.latest_run:
        win = latest_run_window(args.log_dir)
        if win:
            since, until = win["start_ts"], win["stop_ts"]

    fixtures = None
    if args.fixtures:
        with open(args.fixtures) as f:
            fixtures = json.load(f)

    summary = backfill(
        log_dir=args.log_dir, since=since, until=until, force=args.force,
        limit=args.limit, sleep_s=args.sleep, fixtures=fixtures,
    )
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for k, v in summary.items():
            print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
