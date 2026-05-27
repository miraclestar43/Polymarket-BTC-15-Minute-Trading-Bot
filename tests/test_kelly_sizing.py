"""
Safe tests for Kelly criterion position sizing.

These tests prove:
  - p_model (strategy probability) is used for Kelly, NOT market price
  - entry_price is used ONLY to compute odds (b)
  - p_model == entry_price → Kelly ≈ 0 (no edge)
  - p_model > entry_price → positive Kelly (edge detected)
  - p_model < entry_price → negative Kelly (no trade)
  - BUY NO uses p_model for NO win probability
  - missing p_model → safe fallback or skip

These tests:
  - Do NOT place real orders
  - Do NOT require API keys or wallet access
  - Do NOT modify any external state
  - Can be run offline
"""
import sys
import os
from pathlib import Path
from decimal import Decimal

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from execution.risk_engine import calculate_kelly_size, odds_from_price


# ---------------------------------------------------------------------------
# Tests proving p_model vs entry_price distinction
# ---------------------------------------------------------------------------

def test_p_model_equals_entry_price_no_edge():
    """
    When p_model == entry_price for BUY YES, Kelly ≈ 0 (no edge).
    This proves we do NOT use market price as p_model.
    
    BUY YES at entry_price=0.70:
      b = (1-0.70)/0.70 = 0.4286
      If p_model = 0.70 (= entry_price):
        f* = 0.70 - 0.30/0.4286 = 0.70 - 0.70 = 0.00
      Kelly = 0 → no trade
    """
    entry_price = 0.70
    p_model = 0.70  # Same as entry price = no edge
    b = odds_from_price(entry_price, side="long")
    
    size, reason = calculate_kelly_size(
        probability=p_model,
        odds_b=b,
        bankroll=100.0,
        kelly_fraction_cap=0.05,
        min_bet=1.00,
        max_bet=50.00,
    )
    assert float(size) == 0.0, f"Expected $0 (no edge), got ${size}"
    assert "negative_kelly" in reason
    print(f"  ✓ p_model == entry_price → no edge: ${size} ({reason})")


def test_p_model_greater_than_entry_price_positive():
    """
    When p_model > entry_price for BUY YES, Kelly is positive (edge detected).
    
    BUY YES at entry_price=0.60:
      b = (1-0.60)/0.60 = 0.6667
      If p_model = 0.75 (> entry_price):
        f* = 0.75 - 0.25/0.6667 = 0.75 - 0.375 = 0.375
      Positive Kelly → bet
    """
    entry_price = 0.60
    p_model = 0.75  # Higher than entry price = edge
    b = odds_from_price(entry_price, side="long")
    
    size, reason = calculate_kelly_size(
        probability=p_model,
        odds_b=b,
        bankroll=100.0,
        kelly_fraction_cap=0.05,
        min_bet=1.00,
        max_bet=50.00,
    )
    assert float(size) > 0, f"Expected positive size, got ${size}"
    assert "kelly_" in reason
    print(f"  ✓ p_model > entry_price → positive Kelly: ${size} ({reason})")


def test_p_model_less_than_entry_price_no_trade():
    """
    When p_model < entry_price for BUY YES, Kelly is negative (no trade).
    
    BUY YES at entry_price=0.80:
      b = (1-0.80)/0.80 = 0.25
      If p_model = 0.65 (< entry_price):
        f* = 0.65 - 0.35/0.25 = 0.65 - 1.40 = -0.75
      Negative Kelly → no trade
    """
    entry_price = 0.80
    p_model = 0.65  # Lower than entry price = no edge
    b = odds_from_price(entry_price, side="long")
    
    size, reason = calculate_kelly_size(
        probability=p_model,
        odds_b=b,
        bankroll=100.0,
        kelly_fraction_cap=0.05,
        min_bet=1.00,
        max_bet=50.00,
    )
    assert float(size) == 0.0, f"Expected $0 (no edge), got ${size}"
    assert "negative_kelly" in reason
    print(f"  ✓ p_model < entry_price → no trade: ${size} ({reason})")


def test_buy_no_uses_p_model_not_1_minus_price():
    """
    BUY NO uses p_model for NO win probability, NOT 1-entry_price.
    
    BUY NO at entry_price=0.30 (YES price):
      b = entry_price/(1-entry_price) = 0.30/0.70 = 0.4286
      If p_model = 0.75 (model thinks NO wins 75%):
        f* = 0.75 - 0.25/0.4286 = 0.75 - 0.5833 = 0.1667
      Positive Kelly → bet
    
    This is NOT the same as using 1-entry_price (which would be 0.70).
    """
    entry_price = 0.30  # YES price
    p_model = 0.75     # Model's estimate that NO wins
    b = odds_from_price(entry_price, side="short")
    
    size, reason = calculate_kelly_size(
        probability=p_model,
        odds_b=b,
        bankroll=100.0,
        kelly_fraction_cap=0.05,
        min_bet=1.00,
        max_bet=50.00,
    )
    assert float(size) > 0, f"Expected positive size for BUY NO, got ${size}"
    
    # Verify that using 1-entry_price (wrong approach) would give different result
    wrong_p = 1.0 - entry_price  # 0.70 (WRONG: this is market implied, not model)
    wrong_size, _ = calculate_kelly_size(
        probability=wrong_p,
        odds_b=b,
        bankroll=100.0,
        kelly_fraction_cap=0.05,
        min_bet=1.00,
        max_bet=50.00,
    )
    # p_model=0.75 ≠ 1-entry_price=0.70, so sizes should differ
    assert float(size) != float(wrong_size), (
        f"BUY NO with p_model={p_model} should differ from 1-entry_price={wrong_p}"
    )
    print(f"  ✓ BUY NO uses p_model (not 1-entry_price): ${size} ({reason})")


def test_missing_p_model_returns_zero():
    """Missing p_model → no trade (size=0)"""
    size, reason = calculate_kelly_size(
        probability=None,
        odds_b=0.5,
        bankroll=100.0,
        kelly_fraction_cap=0.05,
        min_bet=1.00,
        max_bet=50.00,
    )
    assert float(size) == 0.0
    assert reason == "missing_probability_for_kelly"
    print(f"  ✓ Missing p_model → no trade: ${size} ({reason})")


# ---------------------------------------------------------------------------
# Original tests (kept for regression)
# ---------------------------------------------------------------------------

def test_positive_kelly():
    """Kelly with positive edge: p_model=0.75, b=0.6667 → f*=0.375, capped to 0.05"""
    b = odds_from_price(0.60, side="long")  # b=0.6667
    size, reason = calculate_kelly_size(
        probability=0.75,
        odds_b=b,
        bankroll=100.0,
        kelly_fraction_cap=0.05,
        min_bet=1.00,
        max_bet=50.00,
    )
    # f* = 0.75 - 0.25/0.6667 = 0.375, capped to 0.05 → $5.00
    assert float(size) == 5.00, f"Expected $5.00, got ${size}"
    assert "capped_0.0500" in reason
    print(f"  ✓ Positive Kelly: ${size} ({reason})")


def test_negative_kelly():
    """Kelly with negative edge: p_model=0.40, b=0.5 → f*=-0.80"""
    size, reason = calculate_kelly_size(
        probability=0.40,
        odds_b=0.5,
        bankroll=100.0,
        kelly_fraction_cap=0.05,
        min_bet=1.00,
        max_bet=50.00,
    )
    assert float(size) == 0.0, f"Expected $0, got ${size}"
    assert "negative_kelly" in reason
    print(f"  ✓ Negative Kelly: ${size} ({reason})")


def test_cap_applied():
    """Kelly raw exceeds cap: p_model=0.90, b=2.0 → f*=0.85, capped to 0.05"""
    size, reason = calculate_kelly_size(
        probability=0.90,
        odds_b=2.0,
        bankroll=100.0,
        kelly_fraction_cap=0.05,
        min_bet=1.00,
        max_bet=50.00,
    )
    assert float(size) == 5.00, f"Expected $5.00, got ${size}"
    assert "capped_0.0500" in reason
    print(f"  ✓ Cap applied: ${size} ({reason})")


def test_min_bet_enforcement():
    """Kelly below minimum: p_model=0.55, b=10.0, bankroll=10, cap=0.20 → $2, floored to $10"""
    size, reason = calculate_kelly_size(
        probability=0.55,
        odds_b=10.0,
        bankroll=10.0,
        kelly_fraction_cap=0.20,
        min_bet=10.00,
        max_bet=50.00,
    )
    assert float(size) == 10.00, f"Expected $10.00, got ${size}"
    assert "floored_to_min" in reason
    print(f"  ✓ Min bet enforced: ${size} ({reason})")


def test_max_bet_enforcement():
    """Kelly fraction capped to exactly max_bet: p_model=0.95, b=5.0, cap=0.50 → $50"""
    size, reason = calculate_kelly_size(
        probability=0.95,
        odds_b=5.0,
        bankroll=100.0,
        kelly_fraction_cap=0.50,
        min_bet=1.00,
        max_bet=50.00,
    )
    assert float(size) == 50.00, f"Expected $50.00, got ${size}"
    assert "capped_0.5000" in reason
    print(f"  ✓ Max bet enforced (cap=0.50): ${size} ({reason})")


def test_max_bet_capped_to_max():
    """Kelly result exceeds max_bet via bankroll: p_model=0.90, b=2.0, bankroll=200 → $100 → capped to $50"""
    size, reason = calculate_kelly_size(
        probability=0.90,
        odds_b=2.0,
        bankroll=200.0,
        kelly_fraction_cap=0.50,
        min_bet=1.00,
        max_bet=50.00,
    )
    assert float(size) == 50.00, f"Expected $50.00, got ${size}"
    assert "capped_to_max" in reason
    print(f"  ✓ Max bet capped to max: ${size} ({reason})")


def test_invalid_probability_none():
    """None probability → no trade"""
    size, reason = calculate_kelly_size(
        probability=None,
        odds_b=0.5,
        bankroll=100.0,
        kelly_fraction_cap=0.05,
        min_bet=1.00,
        max_bet=50.00,
    )
    assert float(size) == 0.0
    assert reason == "missing_probability_for_kelly"
    print(f"  ✓ None probability: ${size} ({reason})")


def test_invalid_probability_zero():
    """Zero probability → no trade"""
    size, reason = calculate_kelly_size(
        probability=0.0,
        odds_b=0.5,
        bankroll=100.0,
        kelly_fraction_cap=0.05,
        min_bet=1.00,
        max_bet=50.00,
    )
    assert float(size) == 0.0
    assert "invalid_probability" in reason
    print(f"  ✓ Zero probability: ${size} ({reason})")


def test_invalid_probability_one():
    """Probability = 1.0 → no trade (out of range)"""
    size, reason = calculate_kelly_size(
        probability=1.0,
        odds_b=0.5,
        bankroll=100.0,
        kelly_fraction_cap=0.05,
        min_bet=1.00,
        max_bet=50.00,
    )
    assert float(size) == 0.0
    assert "invalid_probability" in reason
    print(f"  ✓ Probability=1.0: ${size} ({reason})")


def test_odds_from_price_long():
    """BUY YES at 0.70 → b = 0.30/0.70 = 0.4286"""
    b = odds_from_price(0.70, side="long")
    assert b is not None
    assert abs(b - 0.30 / 0.70) < 0.001
    print(f"  ✓ Odds long @0.70: b={b:.4f}")


def test_odds_from_price_short():
    """BUY NO at 0.30 → b = 0.30/0.70 = 0.4286"""
    b = odds_from_price(0.30, side="short")
    assert b is not None
    assert abs(b - 0.30 / 0.70) < 0.001
    print(f"  ✓ Odds short @0.30: b={b:.4f}")


def test_odds_from_price_invalid():
    """Invalid prices → None"""
    assert odds_from_price(0.0, side="long") is None
    assert odds_from_price(1.0, side="long") is None
    assert odds_from_price(-0.1, side="long") is None
    assert odds_from_price(0.5, side="invalid") is None
    print(f"  ✓ Invalid prices return None")


def test_realistic_btc_scenario():
    """
    Realistic BTC 15-min scenario:
    Entry price = 0.72 (market says 72% YES)
    Our model says 78% YES (p_model = 0.78, we think we have edge)
    
    BUY YES:
      b = (1-0.72)/0.72 = 0.3889
      f* = 0.78 - 0.22/0.3889 = 0.78 - 0.5657 = 0.2143
      Capped to 0.05 → $5.00
    """
    entry_price = 0.72
    p_model = 0.78  # Our model's estimate (higher than market)
    b = odds_from_price(entry_price, side="long")
    
    size, reason = calculate_kelly_size(
        probability=p_model,
        odds_b=b,
        bankroll=100.0,
        kelly_fraction_cap=0.05,
        min_bet=1.00,
        max_bet=50.00,
    )
    assert float(size) == 5.00, f"Expected $5.00, got ${size}"
    print(f"  ✓ Realistic BTC scenario: ${size} ({reason})")


def test_kelly_with_cap_0_05_various():
    """Test Kelly sizing with various p_model values at cap=0.05, bankroll=100"""
    test_cases = [
        # (entry_price, side, p_model, expected_min, expected_max, description)
        (0.60, "long", 0.60, 0.00, 5.00, "p_model=entry_price → no edge"),
        (0.60, "long", 0.70, 0.00, 5.00, "p_model=0.70 > entry=0.60 → edge"),
        (0.70, "long", 0.75, 0.00, 5.00, "p_model=0.75 > entry=0.70 → edge"),
        (0.80, "long", 0.80, 0.00, 5.00, "p_model=entry_price → no edge"),
        (0.30, "short", 0.70, 0.00, 5.00, "BUY NO: p_model=0.70, entry=0.30"),
        (0.30, "short", 0.65, 0.00, 5.00, "BUY NO: p_model=0.65 > 1-entry=0.70? No → edge"),
    ]
    
    for entry_p, side, p_model, exp_min, exp_max, desc in test_cases:
        b = odds_from_price(entry_p, side=side)
        size, reason = calculate_kelly_size(
            probability=p_model,
            odds_b=b,
            bankroll=100.0,
            kelly_fraction_cap=0.05,
            min_bet=1.00,
            max_bet=50.00,
        )
        assert exp_min <= float(size) <= exp_max, (
            f"{desc}: expected [{exp_min}, {exp_max}], got ${size}"
        )
    print(f"  ✓ Various p_model scenarios pass bounds check")


if __name__ == "__main__":
    print("=" * 60)
    print("Kelly Criterion Position Sizing — Safe Tests")
    print("=" * 60)
    
    tests = [
        # Core p_model vs entry_price tests
        test_p_model_equals_entry_price_no_edge,
        test_p_model_greater_than_entry_price_positive,
        test_p_model_less_than_entry_price_no_trade,
        test_buy_no_uses_p_model_not_1_minus_price,
        test_missing_p_model_returns_zero,
        # Regression tests
        test_positive_kelly,
        test_negative_kelly,
        test_cap_applied,
        test_min_bet_enforcement,
        test_max_bet_enforcement,
        test_max_bet_capped_to_max,
        test_invalid_probability_none,
        test_invalid_probability_zero,
        test_invalid_probability_one,
        test_odds_from_price_long,
        test_odds_from_price_short,
        test_odds_from_price_invalid,
        test_realistic_btc_scenario,
        test_kelly_with_cap_0_05_various,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  ✗ FAILED: {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ ERROR: {test.__name__}: {e}")
            failed += 1
    
    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed, {len(tests)} total")
    print("=" * 60)
    
    sys.exit(0 if failed == 0 else 1)
