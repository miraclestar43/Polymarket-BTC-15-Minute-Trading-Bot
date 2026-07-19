"""
Strategy attribution report tests — DRY-RUN / DIAGNOSTIC ONLY.

Deterministic, offline. Verifies:
  * Wilson CI math
  * as-recorded (random) baseline is kept separate from settlement baseline
  * settlement baseline uses real outcomes + binary payout
  * markdown renders required sections
  * paired-variant section present
"""
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

import strategy_attribution_report as rep

_COND_A = "0x" + "a" * 64
_COND_B = "0x" + "b" * 64
_COND_C = "0x" + "c" * 64
_SLUG_A = "btc-updown-15m-1782000000"
_SLUG_B = "btc-updown-15m-1782000900"
_SLUG_C = "btc-updown-15m-1782001800"
_END_A, _END_B, _END_C = 1782000900, 1782001800, 1782002700


def test_wilson_ci():
    assert rep.wilson_ci(0, 0) is None
    ci = rep.wilson_ci(5, 10)
    assert ci["p"] == 0.5
    assert 0.0 < ci["low"] < 0.5 < ci["high"] < 1.0
    ci2 = rep.wilson_ci(10, 10)
    assert ci2["p"] == 1.0
    assert ci2["high"] <= 1.0 and ci2["low"] < 1.0  # never a false certainty


def _mk_shadow(cond, slug, side, entry, ts):
    return {
        "timestamp": ts, "config_name": "baseline",
        "market_id": f"{cond}-1.POLYMARKET", "market_slug": slug,
        "market_end_time": None, "shadow_side": side, "entry_price": entry,
        "p_model": 0.75, "p_stay": 1.0, "odds_b": 0.4,
        "raw_kelly_fraction": 0.1, "capped_kelly_fraction": 0.05,
        "kelly_gap": 0.05, "btc_return_15m": -0.001,
        "btc_regime": "neutral", "would_trade": True,
    }


def _mk_settlement(cond, slug, end, outcome, source="inferred_from_market_data"):
    return {
        "schema_version": "settlement-1.0", "stable_key": cond,
        "market_id": f"{cond}-1.POLYMARKET", "condition_id": cond, "slug": slug,
        "market_end_ts": end, "resolved_outcome": outcome,
        "resolution_source": source, "resolution_status": "resolved",
    }


def _setup(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    ts = "2026-06-26T01:00:00+00:00"
    # baseline shadow candidates: 1 long (UP win), 1 short (DOWN win), 1 short (UP loss)
    shadow = [
        _mk_shadow(_COND_A, _SLUG_A, "long", 0.7, ts),
        _mk_shadow(_COND_B, _SLUG_B, "short", 0.3, ts),
        _mk_shadow(_COND_C, _SLUG_C, "short", 0.3, ts),
    ]
    (log_dir / "shadow_decisions_2026-06-26.jsonl").write_text(
        "\n".join(json.dumps(r) for r in shadow) + "\n")
    setts = [
        _mk_settlement(_COND_A, _SLUG_A, _END_A, "UP"),
        _mk_settlement(_COND_B, _SLUG_B, _END_B, "DOWN"),
        _mk_settlement(_COND_C, _SLUG_C, _END_C, "UP"),
    ]
    (log_dir / "settlements.jsonl").write_text(
        "\n".join(json.dumps(s) for s in setts) + "\n")
    # as-recorded (random) baseline orders in journal
    orders = [
        {"timestamp": ts, "event_type": "simulated_order", "side": "long", "simulated_pnl": -0.2},
        {"timestamp": ts, "event_type": "simulated_order", "side": "short", "simulated_pnl": 0.3},
        {"timestamp": ts, "event_type": "skipped_trade", "decision_reason": "kelly_no_edge: x"},
    ]
    (log_dir / "trade_journal_2026-06-26.jsonl").write_text(
        "\n".join(json.dumps(r) for r in orders) + "\n")
    return log_dir


def test_build_report_separates_random_and_settlement(tmp_path):
    log_dir = _setup(tmp_path)
    since = 1782434610.0 - 10_000_000  # wide window covering the ts above
    until = 1782434610.0 + 10_000_000
    r = rep.build_report(since, until, "test", log_dir=str(log_dir))

    ar = r["as_recorded_random_baseline"]
    assert "RANDOM" in ar["accounting"]
    assert ar["n_orders"] == 2
    assert abs(ar["total_pnl"] - 0.1) < 1e-9

    sb = r["settlement_baseline"]
    # long: 1 win (UP), short: 1 win (DOWN) + 1 loss (UP)
    assert sb["long"]["wins"] == 1 and sb["long"]["losses"] == 0
    assert sb["short"]["wins"] == 1 and sb["short"]["losses"] == 1
    # payout asymmetry surfaced
    assert sb["short"]["mean_entry_price"] < sb["long"]["mean_entry_price"]

    # base rate present (UP=2, DOWN=1 in this fixture)
    br = r["short_advantage_confounds"]["market_direction_base_rate"]
    assert br["up"] == 2 and br["down"] == 1


def test_render_markdown_has_sections(tmp_path):
    log_dir = _setup(tmp_path)
    r = rep.build_report(0.0, 2e9, "test", log_dir=str(log_dir))
    md = rep.render_markdown(r)
    for section in ("Baseline P/L", "SHORT advantage", "LONG lost",
                    "Filter attribution", "Paired variant", "Caveats"):
        assert section in md
    assert "RANDOM mark-to-market" in md
