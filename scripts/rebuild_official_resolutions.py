#!/usr/bin/env python3
"""
Safe rebuild of the official-resolution store (CLOB) — DRY-RUN / DIAGNOSTIC.

The current logs/official_resolutions.jsonl was produced partly with an older
parser. This tool rebuilds it with the hardened current parser
(``clob-resolution-2``) and only replaces the active store when the rebuild
fully validates — so a network hiccup or an outcome flip can never corrupt the
authoritative outcomes.

Steps
-----
1. Back up the existing store to ``official_resolutions.backup-<ts>.jsonl``.
2. Collect ALL known condition ids (existing store keys ∪ would_trade shadow
   records) and re-fetch each through the reliable CLOB endpoint.
3. Each accepted record must satisfy the parser's validations: requested==
   returned condition id, closed/resolved, exactly one winner, valid Up/Down
   label, both token ids.
4. Write the derived result atomically to ``official_resolutions.rebuilt.jsonl``.
5. Compare old vs new outcomes; report agreements, mismatches (flips),
   regressions (was resolved → now unavailable), and newly-resolved records.
6. Replace the active store ONLY if validation succeeds: every previously
   resolved condition id is still resolved with the SAME outcome and none
   regressed. Otherwise the active store is left untouched.

Read-only against the exchange (HTTP GET only). Never signs/submits an order,
never touches the original decision logs. Offline fixtures supported for tests.

Usage:
    python scripts/rebuild_official_resolutions.py --all
    python scripts/rebuild_official_resolutions.py --since ISO --until ISO
    python scripts/rebuild_official_resolutions.py --fixtures f.json --report-only
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from core.strategy_brain.settlement import extract_condition_id, market_identity  # noqa: E402
from core.strategy_brain.official_resolution import (  # noqa: E402
    OFFICIAL_STORE_FILE,
    STATUS_RESOLVED,
    fetch_official,
    make_fixture_opener,
)
from analyze_strategy_experiments import (  # noqa: E402
    latest_run_window,
    load_shadow_records,
    _parse_when,
)


def _load_store(path: Path) -> Dict[str, Dict[str, Any]]:
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


def _write_atomic(store: Dict[str, Dict[str, Any]], target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w") as f:
        for key in sorted(store):
            f.write(json.dumps(store[key]) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    return target


def _collect_condition_ids(
    old_store: Dict[str, Dict[str, Any]], records: List[Dict[str, Any]]
) -> List[str]:
    seen: Dict[str, None] = {}
    for cid in old_store:
        seen.setdefault(cid, None)
    for rec in records:
        if not rec.get("would_trade"):
            continue
        cid = extract_condition_id(rec.get("market_id")) or market_identity(rec).get("condition_id")
        if cid:
            seen.setdefault(cid.lower(), None)
    return list(seen)


def rebuild(
    log_dir: str = "logs",
    since: Optional[float] = None,
    until: Optional[float] = None,
    sleep_s: float = 0.2,
    fixtures: Optional[Dict[str, Any]] = None,
    apply: bool = True,
) -> Dict[str, Any]:
    log_path = Path(log_dir)
    active = log_path / OFFICIAL_STORE_FILE
    old_store = _load_store(active)

    # 1. Backup the existing store.
    backup_path = None
    if active.exists():
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        backup_path = log_path / f"{OFFICIAL_STORE_FILE}.backup-{ts}"
        shutil.copy2(active, backup_path)

    # 2. Collect condition ids and re-fetch each.
    records = load_shadow_records("", log_dir=log_dir, since=since, until=until)
    cids = _collect_condition_ids(old_store, records)
    opener = make_fixture_opener(fixtures) if fixtures is not None else None

    new_store: Dict[str, Dict[str, Any]] = {}
    status_counts: Counter = Counter()
    for cid in cids:
        rec = fetch_official(cid, opener=opener)
        new_store[cid.lower()] = rec
        status_counts[rec.get("status", "unknown")] += 1
        if opener is None and sleep_s > 0:
            time.sleep(sleep_s)

    # 4. Write the rebuilt result (always, for inspection).
    rebuilt_path = log_path / f"{OFFICIAL_STORE_FILE}.rebuilt.jsonl"
    _write_atomic(new_store, rebuilt_path)

    # 5. Compare old vs new.
    agreements: List[str] = []
    mismatches: List[Dict[str, Any]] = []
    regressions: List[Dict[str, Any]] = []
    newly_resolved: List[str] = []
    for cid, old_rec in old_store.items():
        if old_rec.get("status") != STATUS_RESOLVED:
            continue
        old_out = str(old_rec.get("resolved_outcome") or "").upper()
        new_rec = new_store.get(cid)
        if not new_rec or new_rec.get("status") != STATUS_RESOLVED:
            regressions.append({"condition_id": cid, "old": old_out,
                                "new_status": (new_rec or {}).get("status")})
            continue
        new_out = str(new_rec.get("resolved_outcome") or "").upper()
        if new_out == old_out:
            agreements.append(cid)
        else:
            mismatches.append({"condition_id": cid, "old": old_out, "new": new_out})
    for cid, new_rec in new_store.items():
        if new_rec.get("status") == STATUS_RESOLVED and (
                old_store.get(cid, {}).get("status") != STATUS_RESOLVED):
            newly_resolved.append(cid)

    # 6. Replace active store only on full validation success.
    validation_ok = (not mismatches) and (not regressions)
    replaced = False
    if apply and validation_ok:
        _write_atomic(new_store, active)
        replaced = True

    new_resolved = sum(1 for r in new_store.values()
                       if r.get("status") == STATUS_RESOLVED)
    return {
        "active_store": str(active),
        "backup": str(backup_path) if backup_path else None,
        "rebuilt_file": str(rebuilt_path),
        "condition_ids_checked": len(cids),
        "old_resolved": sum(1 for r in old_store.values()
                            if r.get("status") == STATUS_RESOLVED),
        "new_resolved": new_resolved,
        "agreements": len(agreements),
        "mismatches": mismatches,
        "regressions": regressions,
        "newly_resolved": len(newly_resolved),
        "fetch_status_counts": dict(status_counts),
        "validation_ok": validation_ok,
        "active_store_replaced": replaced,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Safe CLOB official-resolution rebuild.")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--latest-run", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--fixtures", help="offline fixtures JSON (no network)")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--report-only", action="store_true",
                    help="never replace the active store; report + write rebuilt only")
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

    summary = rebuild(
        log_dir=args.log_dir, since=since, until=until, sleep_s=args.sleep,
        fixtures=fixtures, apply=not args.report_only,
    )
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for k, v in summary.items():
            print(f"{k}: {v}")
    if summary["mismatches"] or summary["regressions"]:
        print("WARNING: active store NOT replaced (validation failed) — "
              "existing store left intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
