"""
Paper-execution / VWAP fill tests — DRY-RUN / DIAGNOSTIC, deterministic, offline.

Covers: fixed-risk single-level fill, multi-level VWAP, insufficient liquidity
(partial), partial-vs-unavailable policy, fee vs pre-fee labelling, and the
unavailable (empty book) case. No liquidity is ever fabricated.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.strategy_brain import paper_execution as pe


def test_fixed_risk_single_level_fill():
    # $1 risk buying at 0.50 with ample size → 2 shares, VWAP 0.50, filled.
    r = pe.simulate_fill([(0.50, 100)], requested_stake=1.0)
    assert r["fill_status"] == pe.FILLED
    assert abs(r["filled_shares"] - 2.0) < 1e-6
    assert abs(r["spent"] - 1.0) < 1e-6
    assert abs(r["vwap"] - 0.50) < 1e-6
    assert r["unfilled_stake"] == 0.0
    assert r["pre_fee"] is True and r["fee_source"] == "pre_fee"


def test_multi_level_vwap_fill():
    # First level too small; walk into the second level. Budget $1.
    # 0.40*1 = 0.40 spent, 1 share; remaining 0.60 at 0.50 → 1.2 shares.
    r = pe.simulate_fill([(0.40, 1), (0.50, 100)], requested_stake=1.0)
    assert r["fill_status"] == pe.FILLED
    assert abs(r["spent"] - 1.0) < 1e-6
    assert abs(r["filled_shares"] - (1.0 + 1.2)) < 1e-6
    # VWAP between 0.40 and 0.50
    assert 0.40 < r["vwap"] < 0.50
    assert len(r["ask_levels_used"]) == 2


def test_insufficient_liquidity_partial():
    # Only 1 share at 0.50 available = $0.50 depth for a $1 budget → partial.
    r = pe.simulate_fill([(0.50, 1)], requested_stake=1.0)
    assert r["fill_status"] == pe.PARTIALLY_FILLED
    assert abs(r["spent"] - 0.50) < 1e-6
    assert abs(r["unfilled_stake"] - 0.50) < 1e-6
    assert abs(r["filled_shares"] - 1.0) < 1e-6


def test_partial_policy_unavailable():
    r = pe.simulate_fill([(0.50, 1)], requested_stake=1.0,
                         partial_policy=pe.POLICY_UNAVAILABLE)
    assert r["fill_status"] == pe.UNAVAILABLE
    # depth still recorded honestly
    assert abs(r["filled_shares"] - 1.0) < 1e-6


def test_unavailable_when_no_depth():
    r = pe.simulate_fill([], requested_stake=1.0)
    assert r["fill_status"] == pe.UNAVAILABLE
    assert r["filled_shares"] == 0.0 and r["vwap"] is None
    r2 = pe.simulate_fill(None, requested_stake=1.0)
    assert r2["fill_status"] == pe.UNAVAILABLE


def test_fee_applied_when_rate_given():
    r = pe.simulate_fill([(0.50, 100)], requested_stake=1.0,
                         fee_rate=0.02, fee_source="clob_market",
                         outcome_win=True)
    assert r["pre_fee"] is False and r["fee_source"] == "clob_market"
    # win: gross = shares (2.0); spent 1.0; fees = 0.02*1.0 = 0.02
    assert abs(r["fees"] - 0.02) < 1e-9
    assert abs(r["net_pnl"] - (2.0 - 1.0 - 0.02)) < 1e-9


def test_pre_fee_never_invents_fee():
    r = pe.simulate_fill([(0.50, 100)], requested_stake=1.0, outcome_win=False)
    assert r["pre_fee"] is True
    assert r["fees"] == 0.0
    # loss: net = -spent
    assert abs(r["net_pnl"] + r["spent"]) < 1e-9


def test_invalid_levels_filtered_not_fabricated():
    # zero/negative sizes and out-of-range prices dropped; 1.0 price is invalid.
    r = pe.simulate_fill([(1.0, 10), (0.0, 5), (0.50, 4)], requested_stake=1.0)
    assert r["top_ask"] == 0.50            # the only valid level
    assert abs(r["spent"] - 1.0) < 1e-6
