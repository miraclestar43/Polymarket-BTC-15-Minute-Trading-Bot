"""
Settlement sweeper tests — DRY-RUN / DIAGNOSTIC ONLY.

Deterministic, offline. Covers:
  * sweeper resolves finished markets from would_trade records
  * duplicate sweeper execution is idempotent (resolved markets skipped)
  * partial / interrupted sweeper recovery (prior dataset preserved & resumed)
  * atomic write leaves no .tmp behind
  * original decision logs are never mutated
  * open (unfinished) markets are retried on later sweeps
"""
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

import sweep_settlements as sw
from core.strategy_brain.market_resolver import OUTCOME_INFERRED

_COND_A = "0x" + "a" * 64
_COND_B = "0x" + "b" * 64
_SLUG_A = "btc-updown-15m-1782000000"
_SLUG_B = "btc-updown-15m-1782000900"
_END_A = 1782000000 + 900
_END_B = 1782000900 + 900
_NOW = _END_B + 3600


def _mk_rec(cond, slug, side="long", entry=0.5, ts="2026-06-26T01:00:00+00:00",
            would_trade=True, config="baseline"):
    return {
        "timestamp": ts,
        "config_name": config,
        "market_id": f"{cond}-1.POLYMARKET",
        "market_slug": slug,
        "market_end_time": None,
        "shadow_side": side,
        "entry_price": entry,
        "would_trade": would_trade,
    }


def _write_shadow(log_dir, recs, date="2026-06-26"):
    p = log_dir / f"shadow_decisions_{date}.jsonl"
    with open(p, "a") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    return p


def _prices(log_dir):
    # A rises (UP), B falls (DOWN).
    pts = [
        {"ts": _END_A - 900, "price": 100.0}, {"ts": _END_A, "price": 110.0},
        {"ts": _END_B - 900, "price": 110.0}, {"ts": _END_B, "price": 100.0},
    ]
    p = log_dir / "btc_price_history.jsonl"
    with open(p, "w") as f:
        for pt in pts:
            f.write(json.dumps(pt) + "\n")


def _setup(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write_shadow(log_dir, [_mk_rec(_COND_A, _SLUG_A, "long"),
                            _mk_rec(_COND_B, _SLUG_B, "short")])
    _prices(log_dir)
    return log_dir


def test_sweep_resolves_finished_markets(tmp_path):
    log_dir = _setup(tmp_path)
    summary = sw.sweep(log_dir=str(log_dir), now_ts=_NOW)
    assert summary["markets_in_scope"] == 2
    assert summary["resolved"] == 2
    assert summary["inferred_from_market_data"] == 2
    setts = sw.load_settlements(str(log_dir))
    assert setts[_COND_A]["resolved_outcome"] == "UP"
    assert setts[_COND_B]["resolved_outcome"] == "DOWN"
    for s in setts.values():
        assert s["resolution_source"] == OUTCOME_INFERRED


def test_duplicate_sweep_is_idempotent(tmp_path):
    log_dir = _setup(tmp_path)
    sw.sweep(log_dir=str(log_dir), now_ts=_NOW)
    before = (log_dir / "settlements.jsonl").read_text()
    summary2 = sw.sweep(log_dir=str(log_dir), now_ts=_NOW)
    after = (log_dir / "settlements.jsonl").read_text()
    assert summary2["processed"] == 0
    assert summary2["skipped_already_resolved"] == 2
    assert before == after  # byte-identical -> no duplicate settlements


def test_partial_interrupted_recovery(tmp_path):
    log_dir = _setup(tmp_path)
    # Simulate a prior interrupted sweep that only settled market A.
    prior = {
        "schema_version": "settlement-1.0", "stable_key": _COND_A,
        "market_id": f"{_COND_A}-1.POLYMARKET", "condition_id": _COND_A,
        "slug": _SLUG_A, "market_end_ts": _END_A, "resolved_outcome": "UP",
        "resolution_source": OUTCOME_INFERRED, "resolution_status": "resolved",
    }
    (log_dir / "settlements.jsonl").write_text(json.dumps(prior) + "\n")

    summary = sw.sweep(log_dir=str(log_dir), now_ts=_NOW)
    # A is preserved (skipped), only B is newly processed.
    assert summary["skipped_already_resolved"] == 1
    assert summary["processed"] == 1
    setts = sw.load_settlements(str(log_dir))
    assert setts[_COND_A]["resolved_outcome"] == "UP"
    assert setts[_COND_B]["resolved_outcome"] == "DOWN"


def test_atomic_write_leaves_no_tmp(tmp_path):
    log_dir = _setup(tmp_path)
    sw.sweep(log_dir=str(log_dir), now_ts=_NOW)
    assert not (log_dir / "settlements.jsonl.tmp").exists()


def test_original_logs_not_mutated(tmp_path):
    log_dir = _setup(tmp_path)
    shadow = log_dir / "shadow_decisions_2026-06-26.jsonl"
    before = shadow.read_text()
    sw.sweep(log_dir=str(log_dir), now_ts=_NOW)
    assert shadow.read_text() == before


def test_open_market_retried_on_later_sweep(tmp_path):
    log_dir = _setup(tmp_path)
    # First sweep BEFORE market B ends: B is open, A resolved.
    sw.sweep(log_dir=str(log_dir), now_ts=_END_A + 10)
    setts = sw.load_settlements(str(log_dir))
    assert setts[_COND_A]["resolution_status"] == "resolved"
    assert setts[_COND_B]["resolution_status"] == "open"
    # Later sweep AFTER B ends: B now resolves (open markets are retried).
    summary = sw.sweep(log_dir=str(log_dir), now_ts=_NOW)
    assert summary["processed"] == 1  # only B reprocessed
    setts = sw.load_settlements(str(log_dir))
    assert setts[_COND_B]["resolution_status"] == "resolved"
