"""
Schema-v2 complete economic record tests — DRY-RUN / DIAGNOSTIC, offline.

Ties the dual-book store to the shadow evaluator's forward block: SHORT uses
the NO ask, LONG uses the YES ask, the NO ask is never derived from the YES
midpoint, a missing/stale NO book yields economic_pnl_unavailable, a quote
captured after the decision is flagged, and the paper-fill fields are emitted.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.market_data.dual_book import DualBookStore
from core.strategy_brain import shadow_evaluator as se
from core.strategy_brain import paper_execution as pe


def _quote_with_both_books(now=1000.0, no_ts=1000.0):
    s = DualBookStore()
    s.set_market("0xcond", "YES1", "NO1", "mkt")
    s.apply_snapshot("YES1", [(0.60, 100)], [(0.62, 90)], ts=1000.0)
    s.apply_snapshot("NO1", [(0.37, 100)], [(0.39, 80), (0.41, 120)], ts=no_ts)
    q = s.snapshot_quote(now=now)
    q["paper_fixed_risk"] = 1.0
    return q


def test_short_uses_no_ask_from_book():
    q = _quote_with_both_books()
    f = se._forward_schema_fields("short", 0.61, q)
    assert f["selected_token_id"] == "NO1"
    assert f["selected_exec_price"] == 0.39            # NO ask, executable
    assert f["selected_exec_price_source"] == "no_ask"
    assert f["economic_pnl_unavailable"] is False
    assert f["executable_economic_available"] is True


def test_long_uses_yes_ask_from_book():
    q = _quote_with_both_books()
    f = se._forward_schema_fields("long", 0.61, q)
    assert f["selected_token_id"] == "YES1"
    assert f["selected_exec_price"] == 0.62
    assert f["selected_exec_price_source"] == "yes_ask"


def test_no_ask_never_derived_from_yes_mid():
    # Only YES book present; a short must NOT price off (1 - yes_mid).
    s = DualBookStore()
    s.set_market("0xcond", "YES1", "NO1", "mkt")
    s.apply_snapshot("YES1", [(0.60, 100)], [(0.62, 90)], ts=1000.0)
    q = s.snapshot_quote(now=1000.0)
    f = se._forward_schema_fields("short", 0.61, q)
    assert f["selected_exec_price"] is None
    assert f["economic_pnl_unavailable"] is True
    assert f["economic_pnl_unavailable_reason"] == "no_token_ask_not_captured"
    assert f["no_mid_source"] == "derived_from_yes_mid"   # labelled, NOT executable


def test_stale_no_book_marks_unavailable():
    q = _quote_with_both_books(now=1100.0, no_ts=1000.0)  # NO book 100s old
    f = se._forward_schema_fields("short", 0.61, q)
    assert f["no_book_status"] == "stale"
    assert f["selected_exec_price"] is None
    assert f["economic_pnl_unavailable"] is True


def test_paper_fill_vwap_recorded_for_short():
    q = _quote_with_both_books()
    f = se._forward_schema_fields("short", 0.61, q)
    # $1 risk buying NO at 0.39 (80 shares avail) → fully filled at 0.39.
    assert f["paper_fill_status"] == pe.FILLED
    assert abs(f["paper_vwap"] - 0.39) < 1e-6
    assert f["paper_top_ask"] == 0.39


def test_quote_after_decision_flagged():
    # NO quote timestamped AFTER the decision → non-blocking flag set False.
    q = _quote_with_both_books(now=1000.0, no_ts=1000.0)
    q["no_quote_timestamp"] = 2000.0     # epoch 2000 = 1970-01-01T00:33:20Z
    f = se._forward_schema_fields(
        "short", 0.61, q, decision_timestamp="1970-01-01T00:00:10+00:00")
    assert f["validation_flags"]["quote_before_or_at_decision"] is False


def test_v1_reader_compatibility_core_fields_present():
    # A v2 record still exposes the original core fields a v1 reader relies on.
    q = _quote_with_both_books()
    f = se._forward_schema_fields("long", 0.61, q)
    for key in ("selected_exec_price", "economic_pnl_unavailable",
                "yes_ask", "no_ask", "schema_version"):
        assert key in f
    assert f["schema_version"] == 2
