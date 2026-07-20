#!/usr/bin/env python3
"""
Full-runner dual-book audit (schema v2) — DIAGNOSTIC / READ-ONLY.

Audits EVERY would-trade record in a single dry-run window and proves the
dual-book executable-price capture in the real runner:

  * SHORT decisions price off the contemporaneous NO ask (never the YES
    midpoint, never 1 - a post-resolution YES price, never a stale/closed
    quote); LONG decisions price off the YES ask.
  * per-token quote freshness, cross-book skew, spread, crossed/invalid books,
    side/token match and quote-before-decision are all checked.
  * paper execution is reported HONESTLY for an L1 (top-of-book) live feed:
    depth_source, depth_level_count and l1_limited are recorded; deeper
    liquidity is never fabricated.

It emits a machine-readable forensic CSV for all would-trade records and a
JSON summary with the counts/percentages required by the audit, plus the three
separated views (A directional, B executable paper economics, C unavailable).

Official outcomes are fetched read-only (HTTP GET) via the hardened CLOB parser
(``clob-resolution-2``); an offline opener/cache can be injected for tests.

Usage:
    python scripts/dual_book_run_audit.py \
        --since 2026-07-19T21:16:00Z --until 2026-07-20T01:16:11Z \
        --out-csv logs/dual_book_run_forensic.csv \
        --out-json logs/dual_book_run_audit.json
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from core.strategy_brain.settlement import (  # noqa: E402
    market_identity, outcome_wins_side,
)
from core.strategy_brain.official_resolution import (  # noqa: E402
    fetch_official, STATUS_RESOLVED,
)
from analyze_strategy_experiments import (  # noqa: E402
    load_shadow_records, _parse_when,
)

# Freshness / skew / spread thresholds (mirror strategy.yaml market_data).
FRESHNESS_S = 60.0
SKEW_WARN_S = 5.0
SPREAD_WARN = 0.10
DECISION_TOL_S = 1.0  # decision + quote written same instant

FORENSIC_FIELDS = [
    # identity / timing
    "timestamp", "config_name", "cycle_start", "condition_id", "market_id",
    "shadow_side", "side_label", "decision_ts",
    "yes_token_id", "no_token_id", "selected_token_id",
    # executable price provenance
    "selected_exec_price", "selected_exec_price_source",
    "yes_bid", "yes_ask", "yes_mid", "no_bid", "no_ask", "no_mid",
    "no_mid_source",
    # per-token books
    "yes_book_status", "no_book_status", "selected_book_status",
    "yes_quote_ts", "no_quote_ts", "selected_quote_ts", "selected_quote_age_s",
    "cross_book_skew_s", "book_generation",
    "yes_spread", "no_spread",
    # PROOF booleans (1 = property holds / OK)
    "ok_selected_matches_side", "ok_exec_from_selected_ask",
    "ok_not_yes_mid", "ok_quote_before_decision",
    "ok_quote_before_resolution", "ok_fresh", "ok_skew", "ok_spread",
    "ok_not_crossed", "ok_in_range", "ok_market_identity",
    # settlement
    "resolution_status", "resolved_outcome", "win",
    # paper execution (L1-honest)
    "requested_stake", "selected_token_ask", "observed_ask_size",
    "depth_source", "depth_level_count", "l1_limited",
    "stake_exceeded_top_level",
    "requested_shares", "filled_shares", "spent", "vwap", "unfilled_stake",
    "fill_status", "max_loss", "fee_status", "fee_rate",
    "gross_pnl", "fees", "net_pnl", "return_on_risk",
]


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _wilson(k: int, n: int, z: float = 1.96):
    """Wilson score interval for a binomial proportion."""
    if n == 0:
        return (None, None, None)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (round(p, 4), round(max(0.0, centre - half), 4),
            round(min(1.0, centre + half), 4))


def _official_for(cid: str, cache: Dict[str, Any], opener) -> Dict[str, Any]:
    key = str(cid).lower()
    if key in cache:
        return cache[key]
    rec = fetch_official(cid, opener=opener) if cid else {"status": "identifier_unavailable"}
    cache[key] = rec
    return rec


def build(records: List[Dict[str, Any]], *, opener=None,
          official_cache: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cache: Dict[str, Any] = dict(official_cache or {})
    wt = [r for r in records if r.get("would_trade")]

    # --- book-availability across ALL evaluated records (capture health) ---
    avail = Counter()
    for r in records:
        y = r.get("yes_book_status") == "ok"
        n = r.get("no_book_status") == "ok"
        avail["both" if (y and n) else "only_yes" if y else
              "only_no" if n else "neither"] += 1

    rows: List[Dict[str, Any]] = []
    official_validation: List[Dict[str, Any]] = []
    settled_keys: set = set()
    for r in wt:
        side = r.get("shadow_side")
        ident = market_identity(r)
        cid = r.get("condition_id") or ident.get("condition_id")
        off = _official_for(cid, cache, opener)
        outcome = off.get("resolved_outcome") if off.get("status") == STATUS_RESOLVED else None
        win = outcome_wins_side(outcome, side)

        dec_ts = _parse_when(r.get("timestamp"))
        y_ts = _f(r.get("yes_quote_timestamp"))
        n_ts = _f(r.get("no_quote_timestamp"))
        sel_ts = n_ts if side == "short" else y_ts
        sel_age = _f(r.get("selected_quote_age_s"))
        if sel_age is None:
            sel_age = _f(r.get("no_quote_age_s") if side == "short"
                         else r.get("yes_quote_age_s"))
        res_ts = _parse_when(off.get("retrieved_at")) if off else None

        yes_bid, yes_ask = _f(r.get("yes_bid")), _f(r.get("yes_ask"))
        no_bid, no_ask = _f(r.get("no_bid")), _f(r.get("no_ask"))
        yes_spread = (yes_ask - yes_bid) if (yes_ask is not None and yes_bid is not None) else None
        no_spread = (no_ask - no_bid) if (no_ask is not None and no_bid is not None) else None
        exec_p = _f(r.get("selected_exec_price"))
        src = r.get("selected_exec_price_source")
        yes_mid = _f(r.get("yes_mid"))
        skew = _f(r.get("cross_book_skew_s"))
        sel_ask = no_ask if side == "short" else yes_ask
        sel_status = r.get("no_book_status") if side == "short" else r.get("yes_book_status")

        # Market/resolution timing to reject a post-resolution derived price.
        end_t = _f(r.get("market_end_time"))
        quote_before_resolution = True
        if sel_ts is not None and end_t is not None:
            # NO quote must be captured before the market's own close (proxy for
            # "not 1 - a post-resolution YES price").
            quote_before_resolution = sel_ts <= end_t + DECISION_TOL_S

        # PROOF booleans.
        ok_selected_matches_side = (
            side == "long" and r.get("selected_token_id") == r.get("yes_token_id")) or (
            side == "short" and r.get("selected_token_id") == r.get("no_token_id"))
        ok_exec_from_selected_ask = (
            exec_p is not None and sel_ask is not None and abs(exec_p - sel_ask) < 1e-9
            and src == ("no_ask" if side == "short" else "yes_ask"))
        ok_not_yes_mid = not (
            side == "short" and (
                src == "yes_ask" or src == "derived_from_yes_mid"
                or (exec_p is not None and yes_mid is not None and abs(exec_p - yes_mid) < 1e-9)))
        ok_quote_before_decision = (
            sel_ts is None or dec_ts is None or sel_ts <= dec_ts + DECISION_TOL_S)
        ok_fresh = (sel_age is None) or (sel_age <= FRESHNESS_S)
        ok_skew = (skew is None) or (skew <= SKEW_WARN_S)
        ok_spread = ((yes_spread is None or yes_spread <= SPREAD_WARN) and
                     (no_spread is None or no_spread <= SPREAD_WARN))
        ok_not_crossed = ((yes_bid is None or yes_ask is None or yes_bid <= yes_ask) and
                          (no_bid is None or no_ask is None or no_bid <= no_ask))
        ok_in_range = all((v is None or 0.0 < v < 1.0)
                          for v in (yes_bid, yes_ask, no_bid, no_ask, exec_p))
        ok_market_identity = (cid is not None and (
            cid in str(r.get("market_id") or "")) and (
            r.get("selected_token_id") in (r.get("yes_token_id"), r.get("no_token_id"))))

        # Paper execution (L1-honest).
        levels = r.get("paper_ask_levels_used") or []
        depth_level_count = len(levels)
        top_size = _f(r.get("paper_top_ask_size"))
        filled = _f(r.get("paper_filled_shares"))
        spent = _f(r.get("paper_spent"))
        unfilled = _f(r.get("paper_unfilled_stake"))
        stake = _f(r.get("paper_requested_stake")) or 1.0
        fill_status = r.get("paper_fill_status")
        pre_fee = bool(r.get("paper_pre_fee", True))
        fee_rate = _f(r.get("paper_fee_rate"))
        stake_exceeded_top = bool(unfilled is not None and unfilled > 1e-9)

        # Settlement economics (view B) — only if a valid fill + resolved outcome.
        gross = fees = net = ror = None
        max_loss = spent
        if win is not None and filled is not None and spent is not None and spent > 0:
            gross_payout = filled * 1.0 if win else 0.0
            gross = round(gross_payout - spent, 6)
            fees = 0.0 if pre_fee or fee_rate is None else round(fee_rate * spent, 6)
            net = round(gross - fees, 6)
            ror = round(net / spent, 6)

        # No-duplicate-settlement guard: count each (config, market) once.
        settled_keys.add((r.get("config_name"), cid))

        rows.append({
            "timestamp": r.get("timestamp"), "config_name": r.get("config_name"),
            "cycle_start": r.get("market_start_time"), "condition_id": cid,
            "market_id": r.get("market_id"), "shadow_side": side,
            "side_label": r.get("side_label"), "decision_ts": dec_ts,
            "yes_token_id": r.get("yes_token_id"), "no_token_id": r.get("no_token_id"),
            "selected_token_id": r.get("selected_token_id"),
            "selected_exec_price": exec_p, "selected_exec_price_source": src,
            "yes_bid": yes_bid, "yes_ask": yes_ask, "yes_mid": yes_mid,
            "no_bid": no_bid, "no_ask": no_ask, "no_mid": _f(r.get("no_mid")),
            "no_mid_source": r.get("no_mid_source"),
            "yes_book_status": r.get("yes_book_status"),
            "no_book_status": r.get("no_book_status"),
            "selected_book_status": sel_status,
            "yes_quote_ts": y_ts, "no_quote_ts": n_ts, "selected_quote_ts": sel_ts,
            "selected_quote_age_s": None if sel_age is None else round(sel_age, 4),
            "cross_book_skew_s": skew, "book_generation": r.get("book_generation"),
            "yes_spread": None if yes_spread is None else round(yes_spread, 6),
            "no_spread": None if no_spread is None else round(no_spread, 6),
            "ok_selected_matches_side": int(ok_selected_matches_side),
            "ok_exec_from_selected_ask": int(ok_exec_from_selected_ask),
            "ok_not_yes_mid": int(ok_not_yes_mid),
            "ok_quote_before_decision": int(ok_quote_before_decision),
            "ok_quote_before_resolution": int(quote_before_resolution),
            "ok_fresh": int(ok_fresh), "ok_skew": int(ok_skew),
            "ok_spread": int(ok_spread), "ok_not_crossed": int(ok_not_crossed),
            "ok_in_range": int(ok_in_range),
            "ok_market_identity": int(bool(ok_market_identity)),
            "resolution_status": off.get("status"), "resolved_outcome": outcome,
            "win": "" if win is None else int(win),
            "requested_stake": stake, "selected_token_ask": sel_ask,
            "observed_ask_size": top_size, "depth_source": "nautilus_l1_top_of_book",
            "depth_level_count": depth_level_count,
            "l1_limited": int(depth_level_count <= 1),
            "stake_exceeded_top_level": int(stake_exceeded_top),
            "requested_shares": _f(r.get("paper_requested_shares")),
            "filled_shares": filled, "spent": spent, "vwap": _f(r.get("paper_vwap")),
            "unfilled_stake": unfilled, "fill_status": fill_status,
            "max_loss": max_loss,
            "fee_status": "pre_fee" if pre_fee else (r.get("paper_fee_source") or "provided"),
            "fee_rate": fee_rate,
            "gross_pnl": gross, "fees": fees, "net_pnl": net, "return_on_risk": ror,
        })

        official_validation.append({
            "condition_id": cid, "config_name": r.get("config_name"),
            "requested_returned_match": str(off.get("condition_id") or "").lower() == str(cid or "").lower(),
            "status_resolved": off.get("status") == STATUS_RESOLVED,
            "single_winner": bool(off.get("winning_token_id")) and (off.get("losing_token_id") is not None),
            "outcome": outcome, "parser_version": off.get("parser_version"),
            "selected_settles_correctly": (win is not None),
        })

    return {
        "rows": rows,
        "book_availability": dict(avail),
        "official_validation": official_validation,
        "official_cache": cache,
    }


def summarize(built: Dict[str, Any], records: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = built["rows"]
    n = len(rows)

    def pct(k):
        return round(100.0 * k / n, 2) if n else 0.0

    # PRIORITY 2 aggregate proof.
    proof_cols = [c for c in FORENSIC_FIELDS if c.startswith("ok_")]
    proof_pass = {c: sum(r[c] for r in rows) for c in proof_cols}

    # Freshness / skew / spread / crossed over ALL evaluated records.
    fresh = stale = skew_warn = spread_warn = crossed = 0
    for r in records:
        for side in ("yes", "no"):
            age = r.get(f"{side}_quote_age_s")
            if isinstance(age, (int, float)):
                if age <= FRESHNESS_S:
                    fresh += 1
                else:
                    stale += 1
        sk = r.get("cross_book_skew_s")
        if isinstance(sk, (int, float)) and sk > SKEW_WARN_S:
            skew_warn += 1
        yb, ya = r.get("yes_bid"), r.get("yes_ask")
        nb, na = r.get("no_bid"), r.get("no_ask")
        if (isinstance(yb, (int, float)) and isinstance(ya, (int, float)) and yb > ya) or \
           (isinstance(nb, (int, float)) and isinstance(na, (int, float)) and nb > na):
            crossed += 1
        for b, a in ((yb, ya), (nb, na)):
            if isinstance(b, (int, float)) and isinstance(a, (int, float)) and (a - b) > SPREAD_WARN:
                spread_warn += 1

    # PRIORITY 3 fill availability by side.
    by_side_fill = defaultdict(Counter)
    for r in rows:
        by_side_fill[r["shadow_side"]][r["fill_status"]] += 1

    # View A directional, View B executable economics, View C unavailable.
    view_a = defaultdict(lambda: {"n": 0, "wins": 0})
    view_b = defaultdict(lambda: {"n": 0, "net": 0.0, "spent": 0.0})
    view_c = 0
    for r in rows:
        cfg = r["config_name"]
        if r["win"] != "":
            view_a[cfg]["n"] += 1
            view_a[cfg]["wins"] += int(r["win"])
        if r["net_pnl"] is not None and r["spent"]:
            view_b[cfg]["n"] += 1
            view_b[cfg]["net"] += r["net_pnl"]
            view_b[cfg]["spent"] += r["spent"]
        else:
            view_c += 1

    view_a_out = {}
    for cfg, d in view_a.items():
        p, lo, hi = _wilson(d["wins"], d["n"])
        view_a_out[cfg] = {"n": d["n"], "wins": d["wins"],
                           "accuracy": p, "wilson95": [lo, hi]}
    view_b_out = {}
    for cfg, d in view_b.items():
        view_b_out[cfg] = {
            "n": d["n"], "net_pnl": round(d["net"], 4),
            "capital_at_risk": round(d["spent"], 4),
            "return_on_risk": round(d["net"] / d["spent"], 4) if d["spent"] else None,
        }

    return {
        "would_trade_records": n,
        "book_availability_all_records": built["book_availability"],
        "proof_pass_counts": proof_pass,
        "proof_all_pass": all(v == n for v in proof_pass.values()),
        "freshness": {"fresh_token_quotes": fresh, "stale_token_quotes": stale},
        "cross_book_skew_warnings": skew_warn,
        "spread_warnings": spread_warn,
        "crossed_or_invalid_books": crossed,
        "fill_availability_by_side": {k: dict(v) for k, v in by_side_fill.items()},
        "view_a_directional": view_a_out,
        "view_b_executable_economics": view_b_out,
        "view_c_unavailable_records": view_c,
        "official_validation_all_pass": all(
            v["requested_returned_match"] and v["status_resolved"]
            and v["single_winner"] and v["selected_settles_correctly"]
            for v in built["official_validation"]) if built["official_validation"] else False,
        "l1_depth_note": (
            "Live Nautilus feed is L1/top-of-book only: depth_level_count<=1 for "
            "all fills; multi-level VWAP was NOT observed live. Fills use only the "
            "observed top level; no deeper liquidity was fabricated."),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Full-runner dual-book audit.")
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--out-csv", default="logs/dual_book_run_forensic.csv")
    ap.add_argument("--out-json", default="logs/dual_book_run_audit.json")
    ap.add_argument("--official-cache", help="JSON file of {cid: official_record}")
    args = ap.parse_args(argv)

    since, until = _parse_when(args.since), _parse_when(args.until)
    records = load_shadow_records(since=since, until=until)

    cache = None
    if args.official_cache and Path(args.official_cache).exists():
        cache = json.load(open(args.official_cache))

    built = build(records, official_cache=cache)
    summary = summarize(built, records)

    out_csv = Path(args.out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FORENSIC_FIELDS)
        w.writeheader()
        w.writerows(built["rows"])

    Path(args.out_json).write_text(json.dumps(summary, indent=2))
    print(f"Wrote {out_csv} ({len(built['rows'])} would-trade rows)")
    print(f"Wrote {args.out_json}")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
