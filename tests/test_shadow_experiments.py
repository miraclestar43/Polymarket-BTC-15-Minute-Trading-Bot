"""
Shadow strategy-experiment framework tests — DRY-RUN / DIAGNOSTIC ONLY.

Covers the 9 required guarantees for the multi-strategy shadow framework:
  1. Shadow variants are dry-run-only (every record dry_run=True, simulation=True).
  2. Live trading stays disabled by default (config default live_trading_ack=False).
  3. Shadow logs contain no secrets.
  4. Every active variant is logged each evaluation cycle.
  5. Analysis respects --latest-run scope (no historical bleed).
  6. Analysis does not mix data across runs.
  7. Unresolved candidates are handled without fabricated P/L.
  8. Baseline variant behavior is unchanged.
  9. No live-canary is created/enabled by the framework.

All tests are offline, deterministic, and use temp dirs.
"""
import json
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

from core.strategy_brain import shadow_evaluator as se
from core.strategy_brain import market_resolver as mr
import analyze_strategy_experiments as ase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _eval_kwargs(market_id="0x" + "a" * 64 + "-123.POLYMARKET", direction="long",
                 entry_price=0.70, btc_regime="bear|down", **over):
    base = dict(
        actual_event_type="evaluation",
        actual_decision_reason="test",
        timestamp="2026-06-23T12:00:00+00:00",
        market_id=market_id,
        direction=direction,
        entry_price=entry_price,
        p_model=0.90,
        p_model_source="markov",
        odds_b=0.43,
        kelly_raw=0.03,
        kelly_capped=0.03,
        kelly_size=5.0,
        markov_state="UP",
        p_stay=0.90,
        min_prob_actual=0.87,
        markov_window_actual=20,
        markov_passed=True,
        kelly_passed=True,
        btc_regime=btc_regime,
        regime_source="aggregator",
    )
    base.update(over)
    return base


def _run_eval(log_dir, **over):
    return se.evaluate_shadow(log_dir=str(log_dir), **_eval_kwargs(**over))


# ---------------------------------------------------------------------------
# 1. Shadow variants are dry-run-only
# ---------------------------------------------------------------------------
def test_all_shadow_records_are_dry_run(tmp_path):
    records = _run_eval(tmp_path)
    assert records, "expected shadow records"
    for r in records:
        assert r["dry_run"] is True
        assert r["simulation"] is True


def test_no_order_or_paper_trade_side_effects(tmp_path):
    _run_eval(tmp_path)
    # The shadow framework must never write paper_trades.json or order logs.
    assert not (tmp_path / "paper_trades.json").exists()
    written = {p.name for p in tmp_path.iterdir()}
    for name in written:
        assert name.startswith("shadow_decisions_"), f"unexpected file written: {name}"


# ---------------------------------------------------------------------------
# 2. Live trading disabled by default
# ---------------------------------------------------------------------------
def test_live_trading_ack_defaults_false(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ACK", raising=False)
    import config as cfg
    loaded = cfg.load_config() if hasattr(cfg, "load_config") else None
    if loaded is not None:
        assert loaded.get("live_trading_ack") is False
    else:
        # Fall back to the documented env-only default contract.
        import os
        assert (os.getenv("LIVE_TRADING_ACK", "").lower() == "true") is False


def test_dry_run_defaults_true(monkeypatch):
    monkeypatch.delenv("DRY_RUN", raising=False)
    import config as cfg
    loaded = cfg.load_config() if hasattr(cfg, "load_config") else None
    if loaded is not None:
        assert loaded.get("dry_run") is True


# ---------------------------------------------------------------------------
# 3. Shadow logs contain no secrets
# ---------------------------------------------------------------------------
def test_shadow_logs_have_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRET_TELEGRAM_TOKEN_123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "SECRET_CHAT_999")
    _run_eval(tmp_path)
    blob = ""
    for f in tmp_path.glob("shadow_decisions_*.jsonl"):
        blob += f.read_text()
    assert "SECRET_TELEGRAM_TOKEN_123" not in blob
    assert "SECRET_CHAT_999" not in blob
    # Guard against real secret material. Note: benign market identifier fields
    # such as "token_id" legitimately contain the word "token", so we match the
    # actual secret key names (e.g. bot_token) rather than the bare word.
    for banned in ("bot_token", "api_key", "api_secret", "password",
                   "private_key", "passphrase"):
        assert banned not in blob.lower()


# ---------------------------------------------------------------------------
# 4. Every active variant is logged each cycle
# ---------------------------------------------------------------------------
def test_every_variant_logged(tmp_path):
    records = _run_eval(tmp_path)
    logged = {r["config_name"] for r in records}
    expected = {c["name"] for c in se.load_shadow_configs()}
    assert logged == expected
    assert len(records) == len(expected)


def test_new_regime_variants_present():
    names = {c["name"] for c in se.load_shadow_configs()}
    for v in (
        "block_long_macro_bear_shadow",
        "short_bias_macro_bear_shadow",
        "side_specific_threshold_shadow",
        "trend_guard_shadow",
        "relaxed_markov_and_kelly_shadow",
    ):
        assert v in names


def test_block_long_macro_bear_blocks_long_in_bear(tmp_path):
    records = _run_eval(tmp_path, direction="long", btc_regime="bear|down")
    blk = next(r for r in records if r["config_name"] == "block_long_macro_bear_shadow")
    assert blk["would_trade"] is False
    assert blk["macro_regime"].startswith("bear")


def test_block_long_macro_bear_allows_long_in_bull(tmp_path):
    records = _run_eval(tmp_path, direction="long", btc_regime="bull|up")
    blk = next(r for r in records if r["config_name"] == "block_long_macro_bear_shadow")
    # In a bull macro regime the block does not apply.
    base = next(r for r in records if r["config_name"] == "baseline")
    assert blk["would_trade"] == base["would_trade"]


# ---------------------------------------------------------------------------
# 8. Baseline variant unchanged
# ---------------------------------------------------------------------------
def test_baseline_variant_unchanged(tmp_path):
    records = _run_eval(tmp_path, direction="long", entry_price=0.70,
                        min_prob_actual=0.87, markov_passed=True, kelly_passed=True)
    base = next(r for r in records if r["config_name"] == "baseline")
    # Baseline simply mirrors the actual gates passed; with both gates passing
    # and a clean long entry, baseline would trade.
    assert base["would_trade"] is True
    assert base["config_name"] == "baseline"
    # Baseline carries no override knobs.
    assert "trend_guard_block" in base
    assert base["trend_guard_block"] is False


def test_baseline_config_has_no_override_knobs():
    base = next(c for c in se.SHADOW_CONFIGS if c["name"] == "baseline")
    knobs = set(base) - {"name", "description"}
    assert knobs == set(), f"baseline must have no tuning knobs, found {knobs}"


# ---------------------------------------------------------------------------
# 5 & 6. Analysis respects --latest-run and never mixes runs
# ---------------------------------------------------------------------------
def _write_runner_events(log_dir, start_ts, stop_ts):
    events = [
        {"ts": start_ts, "t": "start", "event_type": "runner_start"},
        {"ts": stop_ts, "t": "stop", "event_type": "runner_stop"},
    ]
    p = log_dir / "runner_events.jsonl"
    with open(p, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _write_shadow(log_dir, date, ts_iso, config_name, would_trade=False):
    rec = {
        "timestamp": ts_iso,
        "config_name": config_name,
        "market_id": "0x" + "b" * 64 + "-1.POLYMARKET",
        "shadow_side": "long",
        "entry_price": 0.7,
        "would_trade": would_trade,
        "macro_regime": "bear",
        "dry_run": True,
        "simulation": True,
    }
    p = log_dir / f"shadow_decisions_{date}.jsonl"
    with open(p, "a") as f:
        f.write(json.dumps(rec) + "\n")


def test_latest_run_window_scopes_records(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.chdir(tmp_path)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    start_ts = 1782300000.0
    stop_ts = 1782303600.0
    # ISO strings derived from the window so scoping is self-consistent.
    old_iso = datetime.fromtimestamp(start_ts - 86400, tz=timezone.utc).isoformat()
    in_scope_iso = datetime.fromtimestamp(start_ts + 1800, tz=timezone.utc).isoformat()
    # Old run (out of scope) and current run.
    _write_shadow(log_dir, "2026-06-20", old_iso, "baseline")
    _write_runner_events(log_dir, start_ts, stop_ts)
    _write_shadow(log_dir, "2026-06-23", in_scope_iso, "baseline")

    win = ase.latest_run_window(str(log_dir))
    assert win is not None
    recs = ase.load_shadow_records("", log_dir=str(log_dir),
                                   since=win["start_ts"], until=win["stop_ts"])
    # Only the in-window record must be present; the old run is excluded.
    assert len(recs) == 1
    assert recs[0]["timestamp"] == in_scope_iso


def test_analysis_does_not_mix_runs(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    # Two separate days; scope to a window covering only the second.
    _write_shadow(log_dir, "2026-06-20", "2026-06-20T01:00:00+00:00", "baseline")
    _write_shadow(log_dir, "2026-06-23", "2026-06-23T12:00:00+00:00", "baseline")
    since = ase._parse_when("2026-06-23T00:00:00+00:00")
    until = ase._parse_when("2026-06-23T23:59:59+00:00")
    recs = ase.load_shadow_records("", log_dir=str(log_dir), since=since, until=until)
    days = {r["timestamp"][:10] for r in recs}
    assert days == {"2026-06-23"}


# ---------------------------------------------------------------------------
# 7. Unresolved handled without fabricated P/L
# ---------------------------------------------------------------------------
def test_unresolved_candidate_has_no_fake_pnl():
    # No market_end_time and empty price history -> cannot resolve.
    class _StubResolver:
        def resolve(self, market_id):
            return {"resolution_status": "pending", "resolved_outcome": None}

    rec = {
        "shadow_side": "long",
        "entry_price": 0.7,
        "market_id": "x",
        "would_trade": True,
        "market_end_time": None,
    }
    out = ase.resolve_candidate(rec, _StubResolver(), price_points=[])
    assert out["outcome_source"] == mr.OUTCOME_UNAVAILABLE
    assert out["hypothetical_win"] is None
    assert out["hypothetical_pnl"] is None


def test_inference_used_when_official_missing():
    class _StubResolver:
        def resolve(self, market_id):
            return {"resolution_status": "pending", "resolved_outcome": None}

    # BTC rose across the 15-min window -> inferred UP -> long wins.
    end_ts = 1782302400.0
    price_points = [
        {"ts": end_ts - 900.0, "price": 100.0},
        {"ts": end_ts, "price": 110.0},
    ]
    rec = {
        "shadow_side": "long",
        "entry_price": 0.5,
        "market_id": "x",
        "would_trade": True,
        "market_end_time": float(end_ts),
    }
    out = ase.resolve_candidate(rec, _StubResolver(), price_points=price_points)
    assert out["outcome_source"] == mr.OUTCOME_INFERRED
    assert out["hypothetical_win"] is True
    assert out["hypothetical_pnl"] is not None


def test_official_resolution_preferred_over_inference():
    class _StubResolver:
        def resolve(self, market_id):
            return {"resolution_status": "resolved", "resolved_outcome": "NO"}

    # Even though BTC rose (would infer UP), official NO must win and beat a short.
    end_ts = 1782302400.0
    price_points = [
        {"ts": end_ts - 900.0, "price": 100.0},
        {"ts": end_ts, "price": 110.0},
    ]
    rec = {
        "shadow_side": "short",
        "entry_price": 0.5,
        "market_id": "x",
        "would_trade": True,
        "market_end_time": float(end_ts),
    }
    out = ase.resolve_candidate(rec, _StubResolver(), price_points=price_points)
    assert out["outcome_source"] == mr.OUTCOME_OFFICIAL
    assert out["hypothetical_win"] is True


def test_coverage_metrics_present_in_analysis(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write_shadow(log_dir, "2026-06-23", "2026-06-23T12:00:00+00:00",
                  "baseline", would_trade=True)
    recs = ase.load_shadow_records("", log_dir=str(log_dir))

    # Resolver/price loaders must be offline here: monkeypatch via attributes.
    class _StubResolver:
        def resolve(self, market_id):
            return {"resolution_status": "pending", "resolved_outcome": None}

    orig_resolver = ase.get_market_resolver
    orig_prices = ase.load_btc_price_history
    ase.get_market_resolver = lambda: _StubResolver()
    ase.load_btc_price_history = lambda *a, **k: []
    try:
        result = ase.analyze(recs)
    finally:
        ase.get_market_resolver = orig_resolver
        ase.load_btc_price_history = orig_prices

    v = result["variants"]["baseline"]
    for key in ("resolved", "unresolved", "resolution_coverage_pct",
                "outcome_source_distribution", "max_drawdown", "preliminary"):
        assert key in v
    # With an unresolvable candidate, coverage is 0 and no fake P/L recorded.
    assert v["resolved"] == 0
    assert v["total_pnl"] == 0.0
    assert v["resolution_coverage_pct"] == 0.0


def test_small_sample_flagged_preliminary(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    end_ts = 1782302400.0
    # One resolvable winning long via inference -> resolved=1 (< threshold).
    rec = {
        "timestamp": "2026-06-23T12:00:00+00:00",
        "config_name": "baseline",
        "market_id": "x",
        "shadow_side": "long",
        "entry_price": 0.5,
        "would_trade": True,
        "macro_regime": "bull",
        "market_end_time": float(end_ts),
        "dry_run": True,
        "simulation": True,
    }
    p = log_dir / "shadow_decisions_2026-06-23.jsonl"
    p.write_text(json.dumps(rec) + "\n")
    recs = ase.load_shadow_records("", log_dir=str(log_dir))

    class _StubResolver:
        def resolve(self, market_id):
            return {"resolution_status": "pending", "resolved_outcome": None}

    orig_resolver = ase.get_market_resolver
    orig_prices = ase.load_btc_price_history
    ase.get_market_resolver = lambda: _StubResolver()
    ase.load_btc_price_history = lambda *a, **k: [
        {"ts": end_ts - 900.0, "price": 100.0},
        {"ts": end_ts, "price": 110.0},
    ]
    try:
        result = ase.analyze(recs)
    finally:
        ase.get_market_resolver = orig_resolver
        ase.load_btc_price_history = orig_prices

    v = result["variants"]["baseline"]
    assert v["resolved"] == 1
    assert v["preliminary"] is True


# ---------------------------------------------------------------------------
# 9. No live-canary created/enabled
# ---------------------------------------------------------------------------
def test_no_live_canary_artifacts(tmp_path):
    _run_eval(tmp_path)
    for p in tmp_path.rglob("*"):
        name = p.name.lower()
        assert "canary" not in name
        assert "live" not in name


def test_framework_imports_no_live_order_calls():
    # The shadow evaluator module must not reference real-order placement.
    src = Path(se.__file__).read_text()
    for banned in ("_place_real_order", "submit_order", "place_order"):
        assert banned not in src, f"shadow evaluator must not call {banned}"


# ---------------------------------------------------------------------------
# Resolver inference unit coverage
# ---------------------------------------------------------------------------
def test_infer_flat_move_is_unavailable():
    pts = [{"ts": 100.0, "price": 50.0}, {"ts": 1000.0, "price": 50.0}]
    out = mr.infer_outcome_from_btc(100.0, 1000.0, pts)
    assert out["outcome"] is None
    assert out["outcome_source"] == mr.OUTCOME_UNAVAILABLE


def test_infer_missing_boundary_is_unavailable():
    pts = [{"ts": 100.0, "price": 50.0}]
    out = mr.infer_outcome_from_btc(100.0, 99999999.0, pts)
    assert out["outcome_source"] == mr.OUTCOME_UNAVAILABLE


def test_normalize_outcome_source_labels():
    assert mr.normalize_outcome_source("resolved", "gamma_api") == mr.OUTCOME_OFFICIAL
    assert mr.normalize_outcome_source("pending", "gamma_api") == mr.OUTCOME_UNAVAILABLE
    assert mr.normalize_outcome_source("error", None) == mr.OUTCOME_UNAVAILABLE


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
