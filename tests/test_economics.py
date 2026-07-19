"""
Economic accounting tests (Part A) — DRY-RUN / DIAGNOSTIC ONLY.

Deterministic, offline (no network). Covers fixed-risk normalization, equal
max-loss across prices, low-price payout artifacts, invalid-price safeguards,
duplicate-decision handling, top-trade concentration, price-floor sensitivity,
drawdown, and profit-factor edge cases. Asserts the module places no orders
and does not import the live bot.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.strategy_brain import economics as econ


# --------------------------------------------------------------------------
# Selected-token exec price mapping (the YES-as-NO regression guard)
# --------------------------------------------------------------------------
def test_selected_token_price_long_uses_yes():
    # A long BUYS the YES token: exec price == the YES price.
    assert econ.selected_token_price("long", 0.30) == 0.30


def test_selected_token_price_short_uses_no_not_yes():
    # A short BUYS the NO token: exec price == 1 - YES, never the YES price.
    assert abs(econ.selected_token_price("short", 0.30) - 0.70) < 1e-12
    assert econ.selected_token_price("short", 0.02) == 0.98  # NOT 0.02


def test_short_settle_prices_no_token():
    # YES=0.02 short win: NO cost 0.98, ~+2% on risk, NOT a ~49x payout.
    r = econ.settle_trade("short", 0.02, "DOWN", sizing_mode=econ.FIXED_RISK, risk=1.0)
    assert r["valid"] and r["win"] is True
    assert r["yes_price"] == 0.02
    assert r["entry_price"] == 0.98              # selected-token exec price
    assert r["net_pnl"] < 0.05                   # nowhere near buggy ~49
    assert abs(r["net_pnl"] - (1 - 0.98) / 0.98) < 1e-6


def test_long_settle_prices_yes_token():
    r = econ.settle_trade("long", 0.02, "UP", sizing_mode=econ.FIXED_RISK, risk=1.0)
    assert r["valid"] and r["win"] is True
    assert r["entry_price"] == 0.02             # long uses YES price directly
    assert abs(r["net_pnl"] - (1 - 0.02) / 0.02) < 1e-4  # ~49x is real for a long


def test_explicit_selected_exec_price_overrides_derivation():
    # When a record stores the taken token's executable price, use it directly.
    r = econ.settle_trade("short", 0.02, "DOWN", sizing_mode=econ.FIXED_RISK,
                          risk=1.0, selected_exec_price=0.97)
    assert r["valid"] and r["entry_price"] == 0.97


def test_model_probability_is_not_an_entry_price():
    # A model probability must never be fed as an entry price. settle_trade only
    # accepts a price via ``entry``/``selected_exec_price``; there is no field
    # by which p_model can silently become the cost. A price >= 1 is rejected.
    ok, reason = econ.validate_price(1.0)
    assert not ok and reason == "price_ge_1"


# --------------------------------------------------------------------------
# Fixed-risk single-trade settlement (LONG / SHORT)
# --------------------------------------------------------------------------
def test_fixed_risk_long_win():
    r = econ.settle_trade("long", 0.5, "UP", sizing_mode=econ.FIXED_RISK, risk=1.0)
    assert r["valid"] and r["win"] is True
    assert r["num_shares"] == 2.0            # 1 / 0.5
    assert r["capital_at_risk"] == 1.0       # == max loss
    assert r["max_loss"] == 1.0
    assert r["net_pnl"] == 1.0               # 2*1 - 1
    assert r["return_on_risk"] == 1.0


def test_fixed_risk_short_win_with_down():
    r = econ.settle_trade("short", 0.5, "DOWN", sizing_mode=econ.FIXED_RISK, risk=1.0)
    assert r["valid"] and r["win"] is True
    assert r["net_pnl"] == 1.0


def test_fixed_risk_long_loss_is_exactly_negative_risk():
    r = econ.settle_trade("long", 0.5, "DOWN", sizing_mode=econ.FIXED_RISK, risk=1.0)
    assert r["valid"] and r["win"] is False
    assert r["net_pnl"] == -1.0              # loss == -R
    assert r["max_loss"] == 1.0


def test_short_wins_only_on_down_not_up():
    win_up = econ.settle_trade("short", 0.4, "UP", sizing_mode=econ.FIXED_RISK)
    assert win_up["win"] is False
    win_down = econ.settle_trade("short", 0.4, "DOWN", sizing_mode=econ.FIXED_RISK)
    assert win_down["win"] is True


# --------------------------------------------------------------------------
# Equal max loss across very different prices (fixed-risk invariance)
# --------------------------------------------------------------------------
def test_equal_max_loss_across_prices():
    for entry in (0.02, 0.1, 0.5, 0.9, 0.98):
        loss = econ.settle_trade("long", entry, "DOWN", sizing_mode=econ.FIXED_RISK, risk=1.0)
        assert loss["max_loss"] == 1.0, entry           # identical risk
        assert loss["net_pnl"] == -1.0, entry


def test_low_price_winner_pays_large_multiple():
    # entry 0.01 win -> ~99x the risk (the longshot artifact source)
    r = econ.settle_trade("long", 0.01, "UP", sizing_mode=econ.FIXED_RISK, risk=1.0)
    assert r["num_shares"] == 100.0
    assert r["net_pnl"] == 99.0
    assert r["return_on_risk"] == 99.0


# --------------------------------------------------------------------------
# Sizing-mode differences
# --------------------------------------------------------------------------
def test_recorded_stake_mode_uses_stake_as_max_loss():
    r = econ.settle_trade("long", 0.25, "DOWN", sizing_mode=econ.RECORDED_STAKE, stake=5.0)
    assert r["capital_at_risk"] == 5.0
    assert r["net_pnl"] == -5.0


def test_equal_shares_mode_has_price_dependent_risk():
    lo = econ.settle_trade("long", 0.1, "DOWN", sizing_mode=econ.EQUAL_SHARES, shares=1.0)
    hi = econ.settle_trade("long", 0.9, "DOWN", sizing_mode=econ.EQUAL_SHARES, shares=1.0)
    assert lo["capital_at_risk"] == 0.1 and hi["capital_at_risk"] == 0.9  # UNEQUAL risk


# --------------------------------------------------------------------------
# Invalid prices / undecidable outcomes are explicit, never silent zeros
# --------------------------------------------------------------------------
def test_invalid_prices_rejected():
    for bad, reason in [(0.0, "price_le_0"), (-0.1, "price_le_0"),
                        (1.0, "price_ge_1"), (1.5, "price_ge_1"),
                        (None, "missing_price"), ("abc", "non_numeric_price"),
                        (float("nan"), "nan_price")]:
        r = econ.settle_trade("long", bad, "UP", sizing_mode=econ.FIXED_RISK)
        assert r["valid"] is False and r["reason"] == reason, (bad, r)
        assert r["net_pnl"] is None


def test_undecidable_outcome_flagged():
    r = econ.settle_trade("long", 0.5, None, sizing_mode=econ.FIXED_RISK)
    assert r["valid"] is False and r["reason"] == "undecidable_outcome"


def test_validate_price_boundaries():
    assert econ.validate_price(0.5) == (True, None)
    assert econ.validate_price(0.0)[0] is False
    assert econ.validate_price(1.0)[0] is False


# --------------------------------------------------------------------------
# Duplicate decisions contribute at most once
# --------------------------------------------------------------------------
def test_dedupe_trades_keeps_first():
    trades = [
        {"stable_key": "a", "side": "long", "entry_price": 0.5, "outcome": "UP"},
        {"stable_key": "a", "side": "long", "entry_price": 0.5, "outcome": "UP"},
        {"stable_key": "b", "side": "short", "entry_price": 0.4, "outcome": "DOWN"},
    ]
    deduped, dropped = econ.dedupe_trades(trades)
    assert len(deduped) == 2
    assert dropped == {"a": 1}


def test_detect_duplicates():
    assert econ.detect_duplicates(["a", "a", "b", "c", "c", "c"]) == {"a": 2, "c": 3}


# --------------------------------------------------------------------------
# Aggregation, concentration, drawdown, profit factor
# --------------------------------------------------------------------------
def _mk(side, entry, outcome, ts=0):
    return {"side": side, "entry_price": entry, "outcome": outcome, "ts": ts}


def test_top_n_concentration_flags_longshot_dominance():
    # one huge winner + many tiny ones
    pnls = [99.0] + [0.1] * 10
    c = econ.top_n_concentration(pnls, ns=(1, 5))
    assert c["top_1_pnl"] == 99.0
    assert c["top_1_share"] > 0.9          # one trade dominates the sum


def test_top_n_share_none_when_total_non_positive():
    c = econ.top_n_concentration([-1.0, -2.0], ns=(1,))
    assert c["top_1_share"] is None


def test_max_drawdown():
    # cumulative path: +1 (peak 1) -> -3 -> dd = 1 - (-3) = 4
    assert econ.max_drawdown([1.0, -4.0, 2.0]) == 4.0
    assert econ.max_drawdown([1.0, 1.0, 1.0]) == 0.0


def test_safe_profit_factor_no_losses_is_undefined():
    pf = econ.safe_profit_factor([1.0, 2.0, 3.0])
    assert pf["value"] is None and pf["note"] == "undefined_no_losing_trades"


def test_safe_profit_factor_with_losses():
    pf = econ.safe_profit_factor([2.0, -1.0])
    assert pf["value"] == 2.0


def test_aggregate_reports_normalized_metrics():
    trades = [
        _mk("long", 0.5, "UP", 1),      # win: exec 0.5 -> +1
        _mk("long", 0.5, "DOWN", 2),    # loss -1
        _mk("short", 0.25, "DOWN", 3),  # win: NO exec 0.75 -> +0.3333
    ]
    agg = econ.aggregate(trades, sizing_mode=econ.FIXED_RISK, risk=1.0)
    assert agg["resolved_trades"] == 3
    assert agg["wins"] == 2 and agg["losses"] == 1
    assert agg["total_capital_at_risk"] == 3.0
    # short is priced against the NO token (1-0.25=0.75), not the YES price.
    assert abs(agg["net_pnl"] - (1.0 - 1.0 + (1 - 0.75) / 0.75)) < 1e-4
    assert abs(agg["net_pnl"] - 0.333333) < 1e-4
    assert "concentration" in agg and "entry_price_distribution" in agg


def test_aggregate_counts_invalid_separately():
    trades = [_mk("long", 0.5, "UP"), _mk("long", 1.5, "UP"), _mk("long", 0.5, None)]
    agg = econ.aggregate(trades, sizing_mode=econ.FIXED_RISK)
    assert agg["resolved_trades"] == 1
    assert agg["invalid_records"].get("price_ge_1") == 1
    assert agg["invalid_records"].get("undecidable_outcome") == 1


# --------------------------------------------------------------------------
# Price-floor sensitivity exposes longshot dependence
# --------------------------------------------------------------------------
def test_price_floor_sensitivity_removes_low_price_trades():
    trades = [
        _mk("long", 0.01, "UP"),   # longshot winner (+99)
        _mk("long", 0.5, "UP"),    # normal winner (+1)
        _mk("long", 0.5, "DOWN"),  # normal loser (-1)
    ]
    rows = econ.price_floor_sensitivity(
        trades, floors=(0.0, 0.05), sizing_mode=econ.FIXED_RISK, risk=1.0)
    by_floor = {r["price_floor"]: r for r in rows}
    assert by_floor[0.0]["resolved_trades"] == 3
    assert by_floor[0.0]["net_pnl"] == 99.0          # dominated by longshot
    assert by_floor[0.05]["resolved_trades"] == 2    # longshot excluded
    assert by_floor[0.05]["net_pnl"] == 0.0          # 1 - 1


# --------------------------------------------------------------------------
# No trading side effects
# --------------------------------------------------------------------------
def test_module_has_no_order_or_bot_imports():
    src = (project_root / "core" / "strategy_brain" / "economics.py").read_text()
    assert "submit_order" not in src
    assert "import bot" not in src and "from bot" not in src
