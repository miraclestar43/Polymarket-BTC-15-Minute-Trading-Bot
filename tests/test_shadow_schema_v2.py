"""
Shadow record schema v2 tests (forward auditable quote block) — DRY-RUN /
DIAGNOSTIC ONLY. Deterministic, offline (no network, no orders).

Verifies that new shadow records carry a self-describing executable-quote
block that keeps YES / NO / selected-token / model concepts strictly separate,
and that a short with no captured NO ask is explicitly marked
economic_pnl_unavailable instead of reusing the YES price.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.strategy_brain import shadow_evaluator as se


def _fields(direction, entry, quote):
    return se._forward_schema_fields(direction, entry, quote)


def test_schema_version_is_v2():
    f = _fields("long", 0.6, None)
    assert f["schema_version"] == se.SHADOW_SCHEMA_VERSION == 2


def test_long_records_yes_ask_as_selected_exec():
    q = {"yes_bid": 0.60, "yes_ask": 0.62, "yes_token_id": "YES1"}
    f = _fields("long", 0.61, q)
    assert f["selected_token_id"] == "YES1"
    assert f["selected_exec_price"] == 0.62         # YES ask, executable
    assert f["selected_exec_price_source"] == "yes_ask"
    assert f["economic_pnl_unavailable"] is False


def test_short_without_no_book_is_marked_unavailable_not_yes():
    # We are subscribed to the YES book only. A short buys NO; with no NO ask
    # captured, economics must be marked unavailable, NOT priced off the YES.
    q = {"yes_bid": 0.01, "yes_ask": 0.03, "yes_token_id": "YES1", "no_token_id": None}
    f = _fields("short", 0.02, q)
    assert f["selected_exec_price"] is None
    assert f["economic_pnl_unavailable"] is True
    assert f["economic_pnl_unavailable_reason"] == "no_token_ask_not_captured"
    # NO mid is derived (1-YES) and clearly labelled as NOT executable.
    assert abs(f["no_mid"] - (1 - 0.02)) < 1e-9
    assert f["no_mid_source"] == "derived_from_yes_mid"


def test_short_with_no_book_prices_no_token():
    q = {"yes_bid": 0.01, "yes_ask": 0.03, "no_bid": 0.97, "no_ask": 0.99,
         "no_token_id": "NO1"}
    f = _fields("short", 0.02, q)
    assert f["selected_token_id"] == "NO1"
    assert f["selected_exec_price"] == 0.99         # NO ask
    assert f["selected_exec_price_source"] == "no_ask"
    assert f["economic_pnl_unavailable"] is False


def test_no_quote_never_backfilled_from_missing_data():
    f = _fields("short", None, None)
    assert f["no_mid"] is None
    assert f["no_mid_source"] == "unavailable"
    assert f["economic_pnl_unavailable"] is True


def test_evaluate_shadow_emits_v2_block(tmp_path):
    recs = se.evaluate_shadow(
        actual_event_type="skipped_trade",
        actual_decision_reason="unit_test",
        timestamp="2026-07-18T00:00:00+00:00",
        market_id="0xabc-123.POLYMARKET",
        direction="short",
        entry_price=0.02,
        p_model=0.83,
        p_model_source="unit",
        odds_b=0.02 / 0.98,
        kelly_raw=0.0, kelly_capped=0.0, kelly_size=0.0,
        markov_state="DOWN", p_stay=0.9,
        min_prob_actual=0.85, markov_window_actual=20,
        markov_passed=True, kelly_passed=False,
        market_slug="btc-updown-15m-1781916300",
        executable_quote={"yes_bid": 0.01, "yes_ask": 0.03, "yes_token_id": "YES1",
                          "quote_timestamp": "2026-07-18T00:00:00+00:00",
                          "quote_source": "yes_book_ws"},
        log_dir=str(tmp_path),
    )
    assert recs, "expected shadow records"
    r0 = recs[0]
    assert r0["schema_version"] == 2
    assert r0["yes_token_id"] == "YES1"
    assert r0["quote_source"] == "yes_book_ws"
    # p_model must never leak into a price field.
    assert r0["selected_exec_price"] != r0["p_model"]
