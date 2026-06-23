#!/usr/bin/env python3
"""
Strategy Experiment (Shadow Variant) Analysis — DRY-RUN / DIAGNOSTIC ONLY.

Compares hypothetical shadow strategy variants recorded in
logs/shadow_decisions_YYYY-MM-DD.jsonl. Scopes to ONE clean run window so
reports never mix historical data:

    python scripts/analyze_strategy_experiments.py --latest-run
    python scripts/analyze_strategy_experiments.py --since ISO --until ISO
    python scripts/analyze_strategy_experiments.py 2026-06-23        # single date
    python scripts/analyze_strategy_experiments.py --all             # all history
    python scripts/analyze_strategy_experiments.py --latest-run --json

Outcome resolution for each `would_trade` shadow candidate:
  1) official_resolution      — Gamma API resolved outcome (preferred)
  2) inferred_from_market_data — BTC price open-vs-close of the 15-min window
  3) unavailable              — no fabricated P/L

Every report shows total / resolved / unresolved / coverage% and the outcome
source distribution. Never places orders. Never prints secrets.
"""
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from core.strategy_brain.market_resolver import (  # noqa: E402
    get_market_resolver,
    normalize_outcome_source,
    load_btc_price_history,
    infer_outcome_from_btc,
    OUTCOME_OFFICIAL,
    OUTCOME_INFERRED,
    OUTCOME_UNAVAILABLE,
)

# Per-variant trade count below which results are flagged PRELIMINARY.
_SMALL_SAMPLE_THRESHOLD = 30
# Fixed notional used for hypothetical P/L so variants are comparable.
_POSITION_SIZE = 5.0


# ---------------------------------------------------------------------------
# Run-window scoping (mirrors run_summary.latest_run_window)
# ---------------------------------------------------------------------------
def _parse_when(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def latest_run_window(log_dir: str = "logs") -> Optional[Dict[str, Any]]:
    path = Path(log_dir) / "runner_events.jsonl"
    if not path.exists():
        return None
    events: List[Dict[str, Any]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    starts = [e for e in events if e.get("event_type") == "runner_start" and e.get("ts")]
    if not starts:
        return None
    start = max(starts, key=lambda e: e.get("ts", 0))
    start_ts = start.get("ts")
    stops = [
        e for e in events
        if e.get("event_type") == "runner_stop" and (e.get("ts") or 0) >= start_ts
    ]
    stop = min(stops, key=lambda e: e.get("ts", 0)) if stops else None
    return {
        "start_ts": start_ts,
        "stop_ts": stop.get("ts") if stop else None,
        "start_t": start.get("t"),
        "stop_t": stop.get("t") if stop else None,
    }


def _record_ts(rec: Dict[str, Any]) -> Optional[float]:
    t = rec.get("timestamp")
    if t is None:
        return None
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def load_shadow_records(
    date_str: str = "",
    log_dir: str = "logs",
    since: Optional[float] = None,
    until: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Load shadow decision records, optionally filtered to [since, until]."""
    records: List[Dict[str, Any]] = []
    log_path = Path(log_dir)
    if date_str:
        files = [log_path / f"shadow_decisions_{date_str}.jsonl"]
    else:
        files = sorted(log_path.glob("shadow_decisions_*.jsonl"))
    for f in files:
        if not f.exists():
            continue
        with open(f) as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since is not None or until is not None:
                    ts = _record_ts(rec)
                    if ts is None:
                        continue
                    if since is not None and ts < since:
                        continue
                    if until is not None and ts > until:
                        continue
                records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Outcome resolution (official -> inferred -> unavailable), no fake P/L
# ---------------------------------------------------------------------------
def _market_window_ts(rec: Dict[str, Any]) -> (Optional[float], Optional[float]):
    """
    Derive the 15-min market (start_ts, end_ts). Prefer explicit market_end_time;
    a BTC 15-min market starts 900s before it ends. Falls back to None.
    """
    end_raw = rec.get("market_end_time")
    end_ts = _parse_when(end_raw) if end_raw else None
    if end_ts is not None:
        return end_ts - 900.0, end_ts
    return None, None


def resolve_candidate(
    rec: Dict[str, Any],
    resolver: Any,
    price_points: list,
) -> Dict[str, Any]:
    """
    Resolve a single would_trade shadow candidate.

    Returns dict with: outcome_source, hypothetical_win (bool|None),
    hypothetical_pnl (float|None), shadow_side.
    """
    side = rec.get("shadow_side", "unknown")
    entry = rec.get("entry_price") or 0.0
    market_id = rec.get("market_id", "")

    outcome = None
    source = OUTCOME_UNAVAILABLE

    # 1) Official resolution via Gamma API.
    try:
        resolution = resolver.resolve(market_id=market_id)
    except Exception:
        resolution = {"resolution_status": "error", "resolved_outcome": None}
    status = resolution.get("resolution_status", "unavailable")
    resolved_outcome = resolution.get("resolved_outcome")
    if status == "resolved" and resolved_outcome:
        outcome = resolved_outcome
        source = OUTCOME_OFFICIAL
    else:
        # 2) BTC price inference fallback.
        start_ts, end_ts = _market_window_ts(rec)
        inf = infer_outcome_from_btc(start_ts, end_ts, price_points)
        if inf["outcome"] is not None:
            outcome = inf["outcome"]
            source = OUTCOME_INFERRED

    if outcome is None:
        return {
            "shadow_side": side,
            "outcome_source": OUTCOME_UNAVAILABLE,
            "hypothetical_win": None,
            "hypothetical_pnl": None,
        }

    # Map outcome to win/loss. YES/UP wins for long; NO/DOWN wins for short.
    up = outcome in ("YES", "UP")
    down = outcome in ("NO", "DOWN")
    if side == "long":
        win = up
    elif side == "short":
        win = down
    else:
        win = None

    pnl = None
    if win is not None and entry and 0.0 < entry < 1.0:
        if win:
            pnl = (1.0 - entry) * _POSITION_SIZE / entry
        else:
            pnl = -_POSITION_SIZE

    return {
        "shadow_side": side,
        "outcome_source": source,
        "hypothetical_win": win,
        "hypothetical_pnl": round(pnl, 4) if pnl is not None else None,
    }


def _max_drawdown(pnls: List[float]) -> float:
    """Max peak-to-trough drawdown of a cumulative P/L curve (<= 0)."""
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return round(mdd, 4)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------
def analyze(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    resolver = get_market_resolver()
    price_points = load_btc_price_history()

    variants = sorted({r.get("config_name", "unknown") for r in records})
    by_variant: Dict[str, Any] = {}

    for name in variants:
        recs = [r for r in records if r.get("config_name") == name]
        candidates = [r for r in recs if r.get("would_trade")]
        skip_reasons = Counter(
            r.get("shadow_skip_reason") for r in recs
            if not r.get("would_trade") and r.get("shadow_skip_reason")
        )

        source_dist: Counter = Counter()
        resolved_pnls: List[float] = []
        wins = 0
        long_pnl = short_pnl = 0.0
        long_n = short_n = long_w = short_w = 0
        regime_bucket: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"n": 0, "w": 0, "pnl": 0.0}
        )

        for c in candidates:
            res = resolve_candidate(c, resolver, price_points)
            source_dist[res["outcome_source"]] += 1
            if res["hypothetical_pnl"] is None:
                continue
            pnl = res["hypothetical_pnl"]
            resolved_pnls.append(pnl)
            if res["hypothetical_win"]:
                wins += 1
            if res["shadow_side"] == "long":
                long_n += 1
                long_pnl += pnl
                long_w += 1 if res["hypothetical_win"] else 0
            elif res["shadow_side"] == "short":
                short_n += 1
                short_pnl += pnl
                short_w += 1 if res["hypothetical_win"] else 0
            macro = (c.get("macro_regime") or "unavailable")
            b = regime_bucket[macro]
            b["n"] += 1
            b["w"] += 1 if res["hypothetical_win"] else 0
            b["pnl"] += pnl

        resolved_n = len(resolved_pnls)
        total_candidates = len(candidates)
        coverage = (resolved_n / total_candidates * 100.0) if total_candidates else 0.0

        by_variant[name] = {
            "description": next((r.get("config_description") for r in recs if r.get("config_description")), ""),
            "decisions_logged": len(recs),
            "would_trade_candidates": total_candidates,
            "resolved": resolved_n,
            "unresolved": total_candidates - resolved_n,
            "resolution_coverage_pct": round(coverage, 1),
            "outcome_source_distribution": dict(source_dist),
            "wins": wins,
            "losses": resolved_n - wins,
            "win_rate_pct": round(wins / resolved_n * 100, 1) if resolved_n else 0.0,
            "total_pnl": round(sum(resolved_pnls), 4),
            "avg_pnl_per_trade": round(sum(resolved_pnls) / resolved_n, 4) if resolved_n else 0.0,
            "max_drawdown": _max_drawdown(resolved_pnls),
            "long": {"trades": long_n, "wins": long_w, "pnl": round(long_pnl, 4)},
            "short": {"trades": short_n, "wins": short_w, "pnl": round(short_pnl, 4)},
            "by_macro_regime": {
                k: {"trades": v["n"], "wins": v["w"], "pnl": round(v["pnl"], 4)}
                for k, v in sorted(regime_bucket.items())
            },
            "skip_reasons": dict(skip_reasons.most_common()),
            "preliminary": resolved_n < _SMALL_SAMPLE_THRESHOLD,
        }

    return {"variants": by_variant}


def print_report(result: Dict[str, Any], window_label: Optional[str]) -> None:
    print("=" * 78)
    print("STRATEGY EXPERIMENT ANALYSIS (shadow variants — DRY-RUN / DIAGNOSTIC)")
    print("=" * 78)
    if window_label:
        print(f"SCOPE: {window_label}")
    else:
        print("SCOPE: ALL HISTORY (not scoped to one run — use --latest-run)")
    print("NOTE: hypothetical P/L only; no real orders. Outcome sources: "
          "official_resolution / inferred_from_market_data / unavailable.")
    print()

    variants = result["variants"]
    if not variants:
        print("No shadow decision records in scope.")
        return

    header = (f"  {'variant':<32}{'cand':>5}{'res':>5}{'cov%':>6}"
              f"{'WR%':>6}{'P/L':>9}{'avg':>8}{'mDD':>9}")
    print(header)
    print("  " + "-" * 74)
    for name, d in variants.items():
        flag = "  *PRELIM*" if d["preliminary"] and d["resolved"] > 0 else ""
        print(f"  {name:<32}{d['would_trade_candidates']:>5}{d['resolved']:>5}"
              f"{d['resolution_coverage_pct']:>6.1f}{d['win_rate_pct']:>6.1f}"
              f"${d['total_pnl']:>8.3f}{d['avg_pnl_per_trade']:>8.3f}"
              f"{d['max_drawdown']:>9.3f}{flag}")

    # Detail blocks
    for name, d in variants.items():
        print()
        print(f"--- {name} — {d['description']} ---")
        print(f"  decisions_logged={d['decisions_logged']}  candidates={d['would_trade_candidates']}  "
              f"resolved={d['resolved']}  unresolved={d['unresolved']}  coverage={d['resolution_coverage_pct']}%")
        print(f"  outcome_sources={d['outcome_source_distribution']}")
        print(f"  LONG: {d['long']['trades']} trades, {d['long']['wins']}W, P/L=${d['long']['pnl']:.3f}"
              f"   SHORT: {d['short']['trades']} trades, {d['short']['wins']}W, P/L=${d['short']['pnl']:.3f}")
        if d["by_macro_regime"]:
            print(f"  by_macro_regime: {d['by_macro_regime']}")
        if d["skip_reasons"]:
            top = dict(list(d["skip_reasons"].items())[:4])
            print(f"  top_skip_reasons: {top}")
        if d["preliminary"] and d["resolved"] > 0:
            print(f"  *** PRELIMINARY: only {d['resolved']} resolved trades "
                  f"(< {_SMALL_SAMPLE_THRESHOLD}); NOT statistically reliable. ***")
        elif d["resolved"] == 0:
            print("  (no resolved trades — nothing to conclude)")


def _arg_value(flag: str) -> Optional[str]:
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return None


def main() -> None:
    as_json = "--json" in sys.argv
    since = _parse_when(_arg_value("--since"))
    until = _parse_when(_arg_value("--until"))
    label = None

    if "--latest-run" in sys.argv:
        win = latest_run_window()
        if win is None:
            print("No runner_start events found — cannot infer the latest run.")
            return
        since, until = win["start_ts"], win["stop_ts"]
        label = f"latest run ({win['start_t']} -> {win['stop_t'] or 'OPEN'})"

    # positional date (skip flags / their values)
    date_str = ""
    skip_next = False
    for a in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if a in ("--since", "--until"):
            skip_next = True
            continue
        if a in ("--all", "--latest-run", "--json") or a.startswith("--since=") or a.startswith("--until="):
            continue
        date_str = a
        break

    records = load_shadow_records(date_str, since=since, until=until)
    if since is not None or until is not None:
        if label is None:
            label = f"custom window since={since} until={until}"
    result = analyze(records)

    if as_json:
        result["scope"] = {"label": label, "since": since, "until": until}
        print(json.dumps(result, indent=2))
    else:
        print_report(result, label)


if __name__ == "__main__":
    main()
