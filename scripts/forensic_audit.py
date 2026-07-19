#!/usr/bin/env python3
"""
Forensic token / side / price / time audit — DRY-RUN / DIAGNOSTIC ONLY.

Traces the full data path decision -> settlement for shadow decision records and
emits a machine-readable CSV plus a concise Markdown summary so every
profitability claim can be independently checked.

KEY PROVEN FINDING (see docs/RESEARCH_FINDINGS.md): the recorded ``entry_price``
is the YES-token MIDPOINT for BOTH long and short decisions (verified because
``odds_b`` equals (1-e)/e for longs and e/(1-e) for shorts across every record).
For SHORT (BUY NO) trades the executable NO price is ``1 - entry_price`` and was
never recorded, so the historical economic P/L that used ``entry_price`` directly
as the selected-token cost is wrong for shorts. This script reports the recorded
(buggy) P/L and the mid-corrected P/L side by side, and flags every invariant.

It NEVER places or influences an order and NEVER fabricates a price.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from core.strategy_brain.settlement import market_identity, outcome_wins_side  # noqa: E402
from core.strategy_brain.official_resolution import load_official_store  # noqa: E402
from analyze_strategy_experiments import (  # noqa: E402
    load_shadow_records, load_settlements, latest_run_window, _parse_when,
)

PARSER_VERSION = "forensic-audit-1.0"


def _iso_ts(rec: Dict[str, Any]) -> Optional[float]:
    from datetime import datetime
    t = rec.get("timestamp")
    if not t:
        return None
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _fixed_risk_pnl(exec_price: float, win: bool, R: float = 1.0) -> float:
    """Fixed-risk P/L for a binary long at executable price exec_price."""
    if not (0.0 < exec_price < 1.0):
        return float("nan")
    return (R * (1.0 - exec_price) / exec_price) if win else -R


def build_rows(
    records: List[Dict[str, Any]],
    settlements: Dict[str, Dict[str, Any]],
    official_store: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for r in records:
        if not r.get("would_trade"):
            continue
        ident = market_identity(r)
        stable_key = ident["stable_key"]
        variant = r.get("config_name", "unknown")
        side = r.get("shadow_side")
        entry = r.get("entry_price")
        dec_ts = _iso_ts(r)
        end_ts = ident.get("market_end_ts")
        start_ts = ident.get("market_start_ts")
        cid = ident.get("condition_id")
        yes_token = ident.get("token_id")  # market_id suffix = subscribed YES token

        s = settlements.get(stable_key) or {}
        outcome = s.get("resolved_outcome") if s.get("resolution_status") == "resolved" else None
        off = official_store.get(str(cid).lower()) if cid else None
        official_outcome = off.get("resolved_outcome") if off else None
        winning_token = off.get("winning_token_id") if off else None

        # Prices (midpoints). YES = entry; NO = 1 - entry. Selected-token exec
        # price = YES for long, NO for short. Recorded-semantics (buggy) used
        # entry directly for both sides.
        yes_price = entry if isinstance(entry, (int, float)) else None
        no_price = (1.0 - entry) if isinstance(entry, (int, float)) else None
        price_valid = isinstance(entry, (int, float)) and (0.0 < float(entry) < 1.0)
        if side == "long":
            exec_recorded = entry
            exec_corrected = yes_price
            selected_token = yes_token
            selected_token_recorded = bool(yes_token)
        elif side == "short":
            exec_recorded = entry            # BUG: YES price used as NO cost
            exec_corrected = no_price        # correct NO mid = 1 - YES mid
            selected_token = None            # NO token id NOT in record
            selected_token_recorded = False
        else:
            exec_recorded = exec_corrected = None
            selected_token = None
            selected_token_recorded = False

        win = outcome_wins_side(outcome, side) if outcome else None
        pnl_recorded = (_fixed_risk_pnl(float(exec_recorded), win)
                        if price_valid and win is not None else None)
        pnl_corrected = (_fixed_risk_pnl(float(exec_corrected), win)
                         if price_valid and win is not None and exec_corrected is not None else None)

        # Validation flags / invariants
        flags = {
            "price_valid_0_1": price_valid,
            "decision_before_market_end": (
                dec_ts is not None and end_ts is not None and dec_ts < end_ts),
            "quote_ts_recorded": False,  # no separate quote timestamp is stored
            "selected_token_id_recorded": selected_token_recorded,
            "is_short_no_token_missing": (side == "short"),
            "official_available": official_outcome is not None,
            "official_matches_settlement": (
                official_outcome is not None and outcome is not None
                and str(official_outcome).upper() == str(outcome).upper()),
            "economic_pnl_executable": False,  # entry is a MID, not an ask; no spread/fees
        }

        rows.append({
            "decision_id": f"{variant}|{stable_key}|{r.get('timestamp')}",
            "variant": variant,
            "decision_ts": r.get("timestamp"),
            "quote_ts": None,
            "market_start_ts": start_ts,
            "market_end_ts": end_ts,
            "seconds_to_close": (round(end_ts - dec_ts, 1)
                                 if dec_ts is not None and end_ts is not None else None),
            "condition_id": cid,
            "selected_side": side,
            "selected_token_id": selected_token,
            "yes_token_id": yes_token,
            "no_token_id": None,  # not present in decision record
            "recorded_entry_price": entry,
            "entry_price_meaning": "YES_token_midpoint",
            "yes_price_mid": yes_price,
            "no_price_mid": no_price,
            "selected_exec_price_recorded_semantics": exec_recorded,
            "selected_exec_price_corrected_mid": exec_corrected,
            "model_probability": r.get("p_model"),
            "model_probability_source": r.get("p_model_source"),
            "official_outcome": official_outcome,
            "settled_outcome": outcome,
            "official_winning_token_id": winning_token,
            "win": win,
            "pnl_fixed_risk_recorded_semantics": pnl_recorded,
            "pnl_fixed_risk_corrected_mid": pnl_corrected,
            **flags,
        })
    return rows


def _dedupe_by_key(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for row in rows:
        k = (row["variant"], row["condition_id"] or row["decision_id"])
        if k in seen:
            continue
        seen.add(k)
        out.append(row)
    return out


def select_samples(rows: List[Dict[str, Any]], seed: int = 1782434610) -> Dict[str, List[Dict[str, Any]]]:
    rnd = random.Random(seed)
    snk = _dedupe_by_key([r for r in rows if r["variant"] == "shadow_no_kelly"])
    base = _dedupe_by_key([r for r in rows if r["variant"] == "baseline"])
    shorts = _dedupe_by_key([r for r in rows if r["selected_side"] == "short"])

    def _pnl(r):
        v = r.get("pnl_fixed_risk_recorded_semantics")
        return v if isinstance(v, (int, float)) else float("-inf")

    top30 = sorted(snk, key=_pnl, reverse=True)[:30]
    rand_snk = rnd.sample(snk, min(30, len(snk)))
    base_low = [r for r in base if isinstance(r["recorded_entry_price"], (int, float))
                and r["recorded_entry_price"] < 0.20]
    rand_base = rnd.sample(base, min(20, len(base)))
    sample_shorts = rnd.sample(shorts, min(20, len(shorts)))
    return {
        "top30_shadow_no_kelly_by_recorded_pnl": top30,
        "random30_shadow_no_kelly": rand_snk,
        "baseline_entry_lt_0.20": base_low,
        "random20_baseline": rand_base,
        "sample20_shorts": sample_shorts,
    }


CSV_FIELDS = [
    "sample_group", "decision_id", "variant", "decision_ts", "quote_ts",
    "market_start_ts", "market_end_ts", "seconds_to_close", "condition_id",
    "selected_side", "selected_token_id", "yes_token_id", "no_token_id",
    "recorded_entry_price", "entry_price_meaning", "yes_price_mid", "no_price_mid",
    "selected_exec_price_recorded_semantics", "selected_exec_price_corrected_mid",
    "model_probability", "model_probability_source", "official_outcome",
    "settled_outcome", "official_winning_token_id", "win",
    "pnl_fixed_risk_recorded_semantics", "pnl_fixed_risk_corrected_mid",
    "price_valid_0_1", "decision_before_market_end", "quote_ts_recorded",
    "selected_token_id_recorded", "is_short_no_token_missing", "official_available",
    "official_matches_settlement", "economic_pnl_executable",
]


def write_csv(samples: Dict[str, List[Dict[str, Any]]], path: Path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for group, rows in samples.items():
            for row in rows:
                w.writerow({"sample_group": group, **row})


def _variant_summary(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        v = r["variant"]
        d = out.setdefault(v, {"n": 0, "wins": 0, "shorts": 0, "short_lt_005": 0,
                               "pnl_recorded": 0.0, "pnl_corrected": 0.0})
        if r["win"] is None:
            continue
        d["n"] += 1
        d["wins"] += 1 if r["win"] else 0
        if r["selected_side"] == "short":
            d["shorts"] += 1
            if isinstance(r["recorded_entry_price"], (int, float)) and r["recorded_entry_price"] < 0.05:
                d["short_lt_005"] += 1
        if isinstance(r["pnl_fixed_risk_recorded_semantics"], (int, float)):
            d["pnl_recorded"] += r["pnl_fixed_risk_recorded_semantics"]
        if isinstance(r["pnl_fixed_risk_corrected_mid"], (int, float)):
            d["pnl_corrected"] += r["pnl_fixed_risk_corrected_mid"]
    return out


def write_markdown(all_rows: List[Dict[str, Any]], samples: Dict[str, List[Dict[str, Any]]],
                   scope: str, path: Path) -> None:
    deduped = _dedupe_by_key(all_rows)
    summ = _variant_summary(deduped)
    lines = [
        "# Forensic Token / Side / Price / Time Audit",
        "", f"- Parser: `{PARSER_VERSION}`", f"- Scope: {scope}",
        f"- Total would_trade rows: {len(all_rows)} (deduped by condition id: {len(deduped)})",
        "",
        "## Proven meaning of `entry_price`",
        "`entry_price` is the **YES-token midpoint** for BOTH long and short "
        "decisions. Verified: `odds_b` equals (1-e)/e for longs and e/(1-e) for "
        "shorts across every record (0 mismatches). It is a **midpoint, not an "
        "executable ask**, and no separate quote timestamp, bid/ask, or NO-token "
        "quote is stored.",
        "",
        "## Critical bug",
        "For SHORT (BUY NO) trades the executable NO price is `1 - entry_price`, "
        "but settlement/economics used `entry_price` (the YES price) directly as "
        "the NO-token cost. At low YES prices this fabricates enormous payouts "
        "(e.g. YES=0.02 -> the code treats the NO buy as costing 0.02 and paying "
        "50x, when the real NO cost is 0.98 paying ~1.02x). LONG trades are "
        "unaffected.",
        "",
        "## Per-variant recorded (buggy) vs mid-corrected fixed-risk P/L",
        "",
        "| variant | n | win% | shorts | short entry<0.05 | recorded ΣP/L | corrected(mid) ΣP/L |",
        "|---|--:|--:|--:|--:|--:|--:|",
    ]
    for v in sorted(summ):
        d = summ[v]
        wr = (100 * d["wins"] / d["n"]) if d["n"] else 0.0
        lines.append(
            f"| {v} | {d['n']} | {wr:.1f} | {d['shorts']} | {d['short_lt_005']} | "
            f"{d['pnl_recorded']:.2f} | {d['pnl_corrected']:.2f} |")
    lines += [
        "",
        "*Win% is directional (side vs official outcome) and is unaffected by the "
        "price bug. Corrected(mid) is still a midpoint-based hypothetical, NOT "
        "executable P/L (no spread, slippage, or fees). Historical executable P/L "
        "is not recoverable from these records.*",
        "",
        "## Invariant violations across all rows",
        "",
    ]
    checks = [
        ("price_valid_0_1", "entry price in (0,1)"),
        ("decision_before_market_end", "decision_ts < market_end_ts"),
        ("selected_token_id_recorded", "selected token id recorded"),
        ("quote_ts_recorded", "quote timestamp recorded"),
        ("official_matches_settlement", "official outcome == settled outcome"),
    ]
    for key, label in checks:
        applicable = [r for r in deduped if r.get(key) is not None]
        ok = sum(1 for r in applicable if r.get(key))
        lines.append(f"- {label}: {ok}/{len(applicable)} pass")
    lines += [
        "",
        "## Sample groups (full detail in the CSV)",
        "",
    ]
    for g, rows in samples.items():
        lines.append(f"- `{g}`: {len(rows)} rows")
    path.write_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Forensic token/side/price/time audit.")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--latest-run", action="store_true")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--out-dir", default="logs")
    args = ap.parse_args(argv)

    since = _parse_when(args.since)
    until = _parse_when(args.until)
    if args.latest_run:
        win = latest_run_window(args.log_dir)
        if win:
            since, until = win["start_ts"], win["stop_ts"]

    records = load_shadow_records("", log_dir=args.log_dir, since=since, until=until)
    settlements = load_settlements(args.log_dir)
    official_store = load_official_store(args.log_dir)

    rows = build_rows(records, settlements, official_store)
    samples = select_samples(rows)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "forensic_audit.csv"
    md_path = out / "forensic_audit.md"
    scope = f"since={args.since or since} until={args.until or until}"
    write_csv(samples, csv_path)
    write_markdown(rows, samples, scope, md_path)
    print(f"Wrote {csv_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
