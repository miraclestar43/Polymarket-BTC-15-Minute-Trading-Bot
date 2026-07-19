#!/usr/bin/env python3
"""
Read-only dual-book connectivity smoke test (DRY-RUN / DIAGNOSTIC ONLY).

Proves the end-to-end capability that was missing: capturing a REAL,
contemporaneous NO-token executable ask. It discovers the current BTC
15-minute market, subscribes to BOTH outcome-token order books via read-only
HTTP GET, feeds snapshots into the same DualBookStore the bot uses, and prints
redacted quote-health each cycle.

It MUST NOT (and does not): start 15m_bot_runner.py, invoke bot.py --live,
place/sign/submit or simulate submission of an order, enable any service, or
modify any strategy state. It only performs read-only market-data GETs.

Usage:
    python scripts/no_book_smoke_test.py --duration 200 --interval 10
    python scripts/no_book_smoke_test.py --fixtures f.json --once   # offline
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.market_data.clob_client import (  # noqa: E402
    discover_current_market,
    fetch_book,
    make_fixture_opener,
)
from core.market_data.dual_book import DualBookStore  # noqa: E402

MAX_DURATION_SECONDS = 600  # hard safety cap


def _redact(token: Optional[str], keep: int = 10) -> Optional[str]:
    if not token:
        return token
    s = str(token)
    return s if len(s) <= keep else f"{s[:keep]}…({len(s)} chars)"


def _health(store: DualBookStore, now: float) -> Dict[str, Any]:
    snap = store.snapshot_quote(now)
    return {
        "condition_id": _redact(snap.get("condition_id"), 12),
        "yes_token_id": _redact(snap.get("yes_token_id")),
        "no_token_id": _redact(snap.get("no_token_id")),
        "yes_bid": snap.get("yes_observed_bid"),
        "yes_ask": snap.get("yes_observed_ask"),
        "no_bid": snap.get("no_observed_bid"),
        "no_ask": snap.get("no_observed_ask"),
        "yes_book_status": snap.get("yes_book_status"),
        "no_book_status": snap.get("no_book_status"),
        "yes_quote_age_s": (round(snap["yes_quote_age_s"], 2)
                            if snap.get("yes_quote_age_s") is not None else None),
        "no_quote_age_s": (round(snap["no_quote_age_s"], 2)
                           if snap.get("no_quote_age_s") is not None else None),
        "cross_book_skew_s": (round(snap["cross_book_skew_s"], 3)
                              if snap.get("cross_book_skew_s") is not None else None),
        "executable_no_ask": snap.get("no_ask"),   # only set when NO book is OK
        "validation_flags": snap.get("validation_flags"),
    }


def run_smoke(
    duration: float = 200.0,
    interval: float = 10.0,
    fixtures: Optional[Dict[str, Any]] = None,
    once: bool = False,
    now_fn=time.time,
) -> Dict[str, Any]:
    duration = min(float(duration), MAX_DURATION_SECONDS)
    opener = make_fixture_opener(fixtures) if fixtures is not None else None

    disc = discover_current_market(now=now_fn(), opener=opener)
    if disc.get("error"):
        return {"result": "FAIL", "reason": disc["error"], "discovery": disc}

    store = DualBookStore()
    store.set_market(
        condition_id=disc.get("condition_id"),
        yes_token_id=disc.get("yes_token_id"),
        no_token_id=disc.get("no_token_id"),
        market_id=disc.get("slug"),
    )

    cycles = 0
    reconnects = 0
    captured_no_ask = None
    start = now_fn()
    print("=== dual-book connectivity smoke test (read-only) ===")
    print(f"  market slug         : {disc.get('slug')}")
    print(f"  condition id        : {_redact(disc.get('condition_id'), 12)}")
    print(f"  YES token id        : {_redact(disc.get('yes_token_id'))}")
    print(f"  NO  token id        : {_redact(disc.get('no_token_id'))}")

    while True:
        cycles += 1
        now = now_fn()
        for token_id in (disc.get("yes_token_id"), disc.get("no_token_id")):
            if not token_id:
                continue
            book = fetch_book(token_id, opener=opener)
            if book.get("error"):
                # Simulate reconnect handling: invalidate, then resubscribe.
                store.on_disconnect()
                store.on_resubscribe()
                reconnects += 1
                continue
            store.apply_snapshot(
                token_id, book.get("bids") or [], book.get("asks") or [],
                ts=now, source="clob_rest_snapshot")

        health = _health(store, now)
        if health["executable_no_ask"] is not None:
            captured_no_ask = health["executable_no_ask"]
        print(f"  [cycle {cycles}] " + json.dumps(health))

        if once or (now - start) >= duration:
            break
        time.sleep(min(interval, max(0.0, duration - (now - start))))

    result = "PASS" if captured_no_ask is not None else "FAIL"
    summary = {
        "result": result,
        "cycles": cycles,
        "reconnects_observed": reconnects,
        "captured_executable_no_ask": captured_no_ask,
        "condition_id": _redact(disc.get("condition_id"), 12),
        "yes_token_id": _redact(disc.get("yes_token_id")),
        "no_token_id": _redact(disc.get("no_token_id")),
        "final_health": _health(store, now_fn()),
    }
    print(f"  RESULT: {result} (executable NO ask "
          f"{'captured' if captured_no_ask is not None else 'NOT captured'})")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only dual-book smoke test.")
    ap.add_argument("--duration", type=float, default=200.0)
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--once", action="store_true", help="single snapshot cycle")
    ap.add_argument("--fixtures", help="offline fixtures JSON (no network)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    fixtures = None
    if args.fixtures:
        with open(args.fixtures) as f:
            fixtures = json.load(f)

    summary = run_smoke(
        duration=args.duration, interval=args.interval,
        fixtures=fixtures, once=args.once)
    if args.json:
        print(json.dumps(summary, indent=2))
    return 0 if summary.get("result") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
