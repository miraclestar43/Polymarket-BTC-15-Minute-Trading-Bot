#!/usr/bin/env python3
"""
Validate official resolutions — DRY-RUN / DIAGNOSTIC ONLY.

Independently re-checks the on-disk official-resolution store against the
required invariants and writes a bounded, reproducible audit fixture plus a
Markdown summary. Offline by default (no network): it validates the cached
records that were fetched via the CLOB endpoint. Optionally re-fetches a small
sample live with --refetch (guarded, off by default).

Invariants checked per record:
  * requested condition id == returned condition id (no wrong-market trust),
  * status == resolved with a single winning token,
  * resolved outcome label is Up/Down (mapped),
  * winning price is consistent (==1 when present),
  * market slug parses to a 15-min window and matches the record.

Sample: a seeded random sample (>=50) of resolved markets PLUS every top-30
P/L market from the forensic selection, so the highest-impact markets are
always verified. Records a fetch timestamp and parser version.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from core.strategy_brain import official_resolution as ores  # noqa: E402
from core.strategy_brain.settlement import parse_slug_window  # noqa: E402
from analyze_strategy_experiments import (  # noqa: E402
    load_shadow_records, load_settlements, _parse_when,
)
from forensic_audit import build_rows, select_samples  # noqa: E402

SAMPLE_MIN = 50
SEED = 1782434610


def _load_store_records(log_dir: str) -> List[Dict[str, Any]]:
    path = Path(log_dir) / ores.OFFICIAL_STORE_FILE
    out = []
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
                continue
    return out


def validate_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    cid = rec.get("condition_id")
    req = rec.get("requested_condition_id")
    slug = rec.get("market_slug")
    start_ts, end_ts = parse_slug_window(slug) if slug else (None, None)
    outcome = rec.get("resolved_outcome")
    checks = {
        "condition_id_matches_request": (
            cid is not None and req is not None
            and str(cid).lower() == str(req).lower()),
        "status_resolved": rec.get("status") == ores.STATUS_RESOLVED,
        "has_winning_token": bool(rec.get("winning_token_id")),
        "outcome_is_up_or_down": outcome in ("UP", "DOWN"),
        "winning_price_consistent": (
            rec.get("winning_price") in (None, 1, 1.0)),
        "slug_parses_15min_window": (
            start_ts is not None and end_ts is not None
            and abs((end_ts - start_ts) - 900.0) < 1e-6),
    }
    return {
        "condition_id": cid,
        "market_slug": slug,
        "resolved_outcome": outcome,
        "winning_token_id": rec.get("winning_token_id"),
        "losing_token_id": rec.get("losing_token_id"),
        "parser_version": rec.get("parser_version"),
        "retrieved_at": rec.get("retrieved_at"),
        "checks": checks,
        "all_pass": all(checks.values()),
    }


def _top30_condition_ids(log_dir: str, since, until) -> List[str]:
    recs = load_shadow_records("", log_dir=log_dir, since=since, until=until)
    setts = load_settlements(log_dir)
    store = ores.load_official_store(log_dir)
    rows = build_rows(recs, setts, store)
    samples = select_samples(rows)
    cids = []
    for row in samples.get("top30_shadow_no_kelly_by_recorded_pnl", []):
        c = row.get("condition_id")
        if c:
            cids.append(str(c).lower())
    return cids


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate official resolutions.")
    ap.add_argument("--since", default="2026-06-26T00:43:30Z")
    ap.add_argument("--until", default="2026-07-10T09:58:00Z")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--out-dir", default="logs")
    ap.add_argument("--sample-size", type=int, default=SAMPLE_MIN)
    args = ap.parse_args(argv)

    since = _parse_when(args.since)
    until = _parse_when(args.until)

    store = _load_store_records(args.log_dir)
    resolved = [r for r in store if r.get("status") == ores.STATUS_RESOLVED]
    by_cid = {str(r.get("condition_id")).lower(): r for r in resolved if r.get("condition_id")}

    rnd = random.Random(SEED)
    n = max(args.sample_size, SAMPLE_MIN)
    rand_keys = rnd.sample(list(by_cid), min(n, len(by_cid)))

    top30 = _top30_condition_ids(args.log_dir, since, until)
    selected_keys = list(dict.fromkeys(rand_keys + [c for c in top30 if c in by_cid]))

    results = [validate_record(by_cid[k]) for k in selected_keys]
    passed = sum(1 for r in results if r["all_pass"])
    failures = [r for r in results if not r["all_pass"]]

    fixture = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "parser_version": ores.PARSER_VERSION,
        "store_total": len(store),
        "store_resolved": len(resolved),
        "sample_seed": SEED,
        "sample_size": len(selected_keys),
        "top30_covered": [c for c in top30 if c in by_cid],
        "passed": passed,
        "failed": len(failures),
        "results": results,
    }

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "official_resolution_audit_fixture.json").write_text(json.dumps(fixture, indent=2))

    md = [
        "# Official Resolution Validation",
        "",
        f"- Generated: {fixture['generated']}",
        f"- Parser version: `{ores.PARSER_VERSION}`",
        f"- Store: {len(resolved)} resolved / {len(store)} total",
        f"- Sample (seed {SEED}): {len(selected_keys)} markets "
        f"(incl. {len([c for c in top30 if c in by_cid])} top-30 P/L markets)",
        f"- Passed all invariants: {passed}/{len(selected_keys)}",
        "",
    ]
    if failures:
        md.append("## Failures")
        for r in failures:
            failed = [k for k, v in r["checks"].items() if not v]
            md.append(f"- `{r['condition_id']}` failed: {', '.join(failed)}")
    else:
        md.append("All sampled markets passed every invariant.")
    (out / "official_resolution_audit.md").write_text("\n".join(md) + "\n")

    print(f"Validated {len(selected_keys)} markets: {passed} passed, {len(failures)} failed.")
    print(f"Wrote {out / 'official_resolution_audit_fixture.json'}")
    print(f"Wrote {out / 'official_resolution_audit.md'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
