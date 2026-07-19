"""
Corrected-conclusions view B (executable economic) tests — DRY-RUN /
DIAGNOSTIC ONLY. Deterministic, offline.

Verifies that view B is computed ONLY from records carrying a valid
contemporaneous selected-token executable ask (LONG -> YES ask), that SHORT
records without a captured NO ask are reported unavailable (never priced off
the YES midpoint or model probability), and that the distributions are emitted.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

import corrected_conclusions as cc


def _long(exec_price, win, yes_bid=0.60, yes_ask=0.62):
    return {
        "side": "long", "outcome": "UP" if win else "DOWN", "win": win,
        "economic_pnl_unavailable": False, "selected_exec_price": exec_price,
        "yes_bid": yes_bid, "yes_ask": yes_ask,
        "quote_timestamp": "2026-07-19T05:28:03+00:00",
        "decision_timestamp": "2026-07-19T05:28:03+00:00",
    }


def _short_unavailable():
    return {
        "side": "short", "outcome": "DOWN", "win": True,
        "economic_pnl_unavailable": True, "selected_exec_price": None,
        "yes_bid": 0.01, "yes_ask": 0.03,
        "quote_timestamp": "2026-07-19T05:28:03+00:00",
        "decision_timestamp": "2026-07-19T05:28:03+00:00",
    }


def test_b_available_for_long_with_executable_ask():
    b = cc._executable_economic([_long(0.62, True), _long(0.80, False)])
    assert b["status"] == "available"
    assert b["records_with_executable_selected_quote"] == 2
    # winner at 0.62 -> +(1-0.62)/0.62 ; loser -> -1  => net = 0.6129.. - 1
    assert round(b["net_pnl"], 4) == round((1 - 0.62) / 0.62 - 1.0, 4)
    assert b["long"]["n"] == 2 and b["short"] is None
    assert b["executable_entry_price_dist"]["n"] == 2
    assert b["yes_spread_dist"] is not None


def test_b_unavailable_for_short_without_no_ask():
    b = cc._executable_economic([_short_unavailable()])
    assert b["status"] == "unavailable"
    assert b["records_with_executable_selected_quote"] == 0
    assert "never substituted" in b["reason"]


def test_b_excludes_unavailable_shorts_but_keeps_longs():
    b = cc._executable_economic([_long(0.62, True), _short_unavailable()])
    assert b["status"] == "available"
    # only the long contributes; the short is excluded, never priced off YES
    assert b["records_with_executable_selected_quote"] == 1
    assert b["short"] is None
    assert b["long"]["n"] == 1
