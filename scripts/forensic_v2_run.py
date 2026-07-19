#!/usr/bin/env python3
"""
Compact per-trade forensic CSV for a single dry-run window (schema v2).

DIAGNOSTIC / READ-ONLY. Emits one row per would_trade shadow decision in the
window with the executable-quote provenance, timing, settlement and a set of
validation flags. It NEVER fabricates a missing quote and NEVER substitutes a
midpoint for an absent executable ask: records without a valid selected-token
ask are flagged economic_pnl_unavailable=1 and carry an empty exec price.

Usage:
    python scripts/forensic_v2_run.py \
        --since 2026-07-19T05:19:21Z --until 2026-07-19T15:15:00Z \
        --out logs/forensic_v2_run.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from core.strategy_brain.settlement import market_identity, outcome_wins_side  # noqa: E402
from analyze_strategy_experiments import (  # noqa: E402
    load_shadow_records, load_settlements, _parse_when,
)

FIELDS = [
    "timestamp", "config_name", "market_id", "condition_id",
    "market_start_time", "market_end_time", "quote_timestamp",
    "shadow_side", "side_label",
    "yes_token_id", "no_token_id", "selected_token_id",
    "yes_bid", "yes_ask", "yes_mid", "no_bid", "no_ask", "no_mid",
    "selected_exec_price", "selected_exec_price_source",
    "p_model", "entry_price",
    "economic_pnl_unavailable", "economic_pnl_unavailable_reason",
    "resolved_outcome", "win",
    # derived
    "quote_age_s", "seconds_to_close",
    # validation flags (1 = anomaly present)
    "flag_short_uses_yes_ask", "flag_long_not_yes_ask",
    "flag_price_out_of_0_1", "flag_yes_bid_gt_ask",
    "flag_quote_after_decision", "flag_stale_quote_gt60s",
    "flag_decision_after_close", "flag_missing_quote_ts",
    "flag_exec_equals_p_model", "flag_short_pnl_avail_no_price",
]


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_rows(records, settlements):
    rows = []
    for r in records:
        if not r.get("would_trade"):
            continue
        ident = market_identity(r)
        s = settlements.get(ident["stable_key"]) or {}
        outcome = s.get("resolved_outcome")
        side = r.get("shadow_side")
        win = outcome_wins_side(outcome, side)

        dec_ts = _parse_when(r.get("timestamp"))
        q_ts = _parse_when(r.get("quote_timestamp"))
        end_t = _f(r.get("market_end_time"))
        quote_age = (dec_ts - q_ts) if (dec_ts is not None and q_ts is not None) else None
        sec_to_close = (end_t - dec_ts) if (end_t is not None and dec_ts is not None) else None

        yes_bid, yes_ask = _f(r.get("yes_bid")), _f(r.get("yes_ask"))
        exec_p = _f(r.get("selected_exec_price"))
        src = r.get("selected_exec_price_source")
        p_model = _f(r.get("p_model"))
        unavail = bool(r.get("economic_pnl_unavailable", True))

        flags = {
            "flag_short_uses_yes_ask": int(side == "short" and src == "yes_ask"),
            "flag_long_not_yes_ask": int(
                side == "long" and exec_p is not None and src != "yes_ask"),
            "flag_price_out_of_0_1": int(
                exec_p is not None and not (0.0 < exec_p < 1.0)),
            "flag_yes_bid_gt_ask": int(
                yes_bid is not None and yes_ask is not None and yes_bid > yes_ask),
            # 1s tolerance: decision + quote records are written the same instant,
            # so only a genuine look-ahead gap (> 1s after) is an anomaly.
            "flag_quote_after_decision": int(
                quote_age is not None and quote_age < -1.0),
            "flag_stale_quote_gt60s": int(
                quote_age is not None and quote_age > 60),
            "flag_decision_after_close": int(
                sec_to_close is not None and sec_to_close < 0),
            "flag_missing_quote_ts": int(q_ts is None),
            "flag_exec_equals_p_model": int(
                exec_p is not None and p_model is not None
                and abs(exec_p - p_model) < 1e-9),
            "flag_short_pnl_avail_no_price": int(
                side == "short" and not unavail and exec_p is None),
        }

        rows.append({
            "timestamp": r.get("timestamp"),
            "config_name": r.get("config_name"),
            "market_id": r.get("market_id"),
            "condition_id": r.get("condition_id"),
            "market_start_time": r.get("market_start_time"),
            "market_end_time": r.get("market_end_time"),
            "quote_timestamp": r.get("quote_timestamp"),
            "shadow_side": side,
            "side_label": r.get("side_label"),
            "yes_token_id": r.get("yes_token_id"),
            "no_token_id": r.get("no_token_id"),
            "selected_token_id": r.get("selected_token_id"),
            "yes_bid": r.get("yes_bid"), "yes_ask": r.get("yes_ask"),
            "yes_mid": r.get("yes_mid"), "no_bid": r.get("no_bid"),
            "no_ask": r.get("no_ask"), "no_mid": r.get("no_mid"),
            "selected_exec_price": r.get("selected_exec_price"),
            "selected_exec_price_source": src,
            "p_model": r.get("p_model"), "entry_price": r.get("entry_price"),
            "economic_pnl_unavailable": int(unavail),
            "economic_pnl_unavailable_reason": r.get("economic_pnl_unavailable_reason"),
            "resolved_outcome": outcome,
            "win": "" if win is None else int(win),
            "quote_age_s": None if quote_age is None else round(quote_age, 3),
            "seconds_to_close": None if sec_to_close is None else round(sec_to_close, 3),
            **flags,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--out", default="logs/forensic_v2_run.csv")
    args = ap.parse_args()

    since, until = _parse_when(args.since), _parse_when(args.until)
    records = load_shadow_records(since=since, until=until)
    settlements = load_settlements()
    rows = build_rows(records, settlements)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    flag_cols = [c for c in FIELDS if c.startswith("flag_")]
    anomalies = {c: sum(r[c] for r in rows) for c in flag_cols}
    total_anom = sum(anomalies.values())
    unavail = sum(r["economic_pnl_unavailable"] for r in rows)
    print(f"Wrote {out} ({len(rows)} trades)")
    print(f"economic_pnl_unavailable: {unavail}/{len(rows)}")
    print(f"total anomaly flags set: {total_anom}")
    if total_anom:
        for c, n in anomalies.items():
            if n:
                print(f"  {c}: {n}")
    else:
        print("  (no anomalies detected)")


if __name__ == "__main__":
    main()
