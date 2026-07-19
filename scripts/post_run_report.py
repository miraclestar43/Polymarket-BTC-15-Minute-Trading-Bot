#!/usr/bin/env python3
"""
Post-run report — ONE scoped report for an explicit run window. DRY-RUN /
DIAGNOSTIC ONLY. No network, no orders, no strategy changes.

Wraps the three-strictly-separated evaluations (directional / recorded
executable economic / synthetic sensitivity) for a single, explicitly-bounded
window and writes a scoped Markdown + JSON report to the output directory.

Recorded economic P/L is reported as UNAVAILABLE for any record lacking a valid
executable selected-token quote — it is never guessed or substituted with model
probability.

Usage:
  python scripts/post_run_report.py --since 2026-06-26T00:43:30Z \
                                    --until 2026-07-10T09:58:00Z
  python scripts/post_run_report.py --latest-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from corrected_conclusions import build, to_markdown  # noqa: E402
from analyze_strategy_experiments import (  # noqa: E402
    latest_run_window, _parse_when,
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scoped post-run report.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--latest-run", action="store_true",
                   help="Use the most recent recorded run window.")
    g.add_argument("--since", help="ISO start (requires --until).")
    ap.add_argument("--until", help="ISO end (with --since).")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--out-dir", default="logs")
    args = ap.parse_args(argv)

    if args.latest_run:
        win = latest_run_window(args.log_dir)
        if not win:
            print("No run window found in logs.", file=sys.stderr)
            return 2
        since, until = win["start_ts"], win["stop_ts"]
    else:
        if not args.until:
            ap.error("--since requires --until")
        since, until = _parse_when(args.since), _parse_when(args.until)

    report = build(since, until, log_dir=args.log_dir)
    report["window"] = {
        "since": datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
        if since else None,
        "until": datetime.fromtimestamp(until, tz=timezone.utc).isoformat()
        if until else None,
        "generated": datetime.now(timezone.utc).isoformat(),
    }

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md = ("# Post-Run Report (scoped)\n\n"
          f"- Window: {report['window']['since']} → {report['window']['until']}\n"
          f"- Generated: {report['window']['generated']}\n\n"
          + to_markdown(report))
    (out / "post_run_report.json").write_text(json.dumps(report, indent=2))
    (out / "post_run_report.md").write_text(md)
    print(f"Wrote {out / 'post_run_report.json'}")
    print(f"Wrote {out / 'post_run_report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
