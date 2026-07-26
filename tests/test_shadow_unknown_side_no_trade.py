"""
Unknown-side would_trade forward-bug fix — regression test.

Root cause, proven against the 2026-07-20..27 collection run
(logs/shadow_decisions_*.jsonl): when bot.py's real signal is neutral it
passes ``direction="unknown"`` into evaluate_shadow (correctly — there is no
real side that cycle). The Kelly-relaxed shadow paths (skip_kelly /
kelly_min_raw), unlike the standard-Kelly path which is gated by the real
bot's own ``kwargs["kelly_passed"]``, only checked
``shadow_direction != "blocked_by_direction"`` before setting
``kelly_passed_shadow``. A stale/persisted Markov ``p_stay`` from a PRIOR
cycle could still clear the (relaxed) Markov+Kelly gates, so ``would_trade``
came out ``True`` with ``shadow_side="unknown"`` and ``selected_token_id``
null — 48 such records across
shadow_no_kelly / shadow_allow_kelly_minus_002 / shadow_allow_kelly_minus_005
/ relaxed_markov_and_kelly_shadow, and no others.

This does not rewrite any historical record (those stay `would_trade=true`
in the raw logs; the run-level audit already excludes them from directional
and economic metrics because shadow_side is not "long"/"short"). It only
fixes the forward label so future cycles report an honest no-trade.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.strategy_brain import shadow_evaluator as se


def _eval_kwargs(direction="long", p_stay=0.90, kelly_raw=0.03, kelly_passed=True, **over):
    base = dict(
        actual_event_type="evaluation",
        actual_decision_reason="test",
        timestamp="2026-06-23T12:00:00+00:00",
        market_id="0x" + "a" * 64 + "-123.POLYMARKET",
        direction=direction,
        entry_price=0.70,
        p_model=0.90,
        p_model_source="markov",
        odds_b=0.43,
        kelly_raw=kelly_raw,
        kelly_capped=kelly_raw,
        kelly_size=5.0,
        markov_state="UP",
        p_stay=p_stay,
        min_prob_actual=0.87,
        markov_window_actual=20,
        markov_passed=True,
        kelly_passed=kelly_passed,
        btc_regime="bear|down",
        regime_source="aggregator",
    )
    base.update(over)
    return base


_RELAXED_KELLY_VARIANTS = {
    "shadow_no_kelly",
    "shadow_allow_kelly_minus_002",
    "shadow_allow_kelly_minus_005",
    "relaxed_markov_and_kelly_shadow",
}


def _by_variant(records):
    return {r["config_name"]: r for r in records}


def test_unknown_direction_never_trades_in_relaxed_kelly_variants(tmp_path):
    # p_stay well above every variant's min_prob and kelly_raw well above
    # every relaxed threshold: before the fix this alone was enough to set
    # would_trade=True for the relaxed-Kelly variants even with no side.
    records = se.evaluate_shadow(
        log_dir=str(tmp_path), **_eval_kwargs(direction="unknown", p_stay=0.95, kelly_raw=0.5)
    )
    by_variant = _by_variant(records)
    for name in _RELAXED_KELLY_VARIANTS:
        rec = by_variant[name]
        assert rec["shadow_side"] == "unknown"
        assert rec["would_trade"] is False, f"{name} incorrectly would_trade with no side"
        assert rec["shadow_skip_reason"] == "no_valid_side: actual_direction=unknown"
        assert rec["side_label"] == "N/A"


def test_unknown_direction_never_trades_for_any_variant(tmp_path):
    records = se.evaluate_shadow(
        log_dir=str(tmp_path), **_eval_kwargs(direction="unknown", p_stay=0.95, kelly_raw=0.5)
    )
    assert all(r["would_trade"] is False for r in records)


def test_valid_long_side_still_trades_in_relaxed_kelly_variants(tmp_path):
    # Guard against over-fixing: a genuine long signal must still be able to
    # trade in the relaxed-Kelly variants exactly as before.
    records = se.evaluate_shadow(
        log_dir=str(tmp_path),
        **_eval_kwargs(direction="long", p_stay=0.95, kelly_raw=0.5, entry_price=0.70),
    )
    by_variant = _by_variant(records)
    for name in _RELAXED_KELLY_VARIANTS:
        rec = by_variant[name]
        assert rec["shadow_side"] == "long"
        assert rec["would_trade"] is True, f"{name} should still trade a valid long signal"


def test_valid_short_side_still_trades_in_relaxed_kelly_variants(tmp_path):
    records = se.evaluate_shadow(
        log_dir=str(tmp_path),
        **_eval_kwargs(direction="short", p_stay=0.95, kelly_raw=0.5, entry_price=0.30),
    )
    by_variant = _by_variant(records)
    for name in _RELAXED_KELLY_VARIANTS:
        rec = by_variant[name]
        assert rec["shadow_side"] == "short"
        assert rec["would_trade"] is True, f"{name} should still trade a valid short signal"


def test_blocked_by_direction_keeps_its_own_skip_reason(tmp_path):
    # short_only blocks a long signal via a different, pre-existing path;
    # the new no_valid_side branch must not shadow that reason.
    records = se.evaluate_shadow(
        log_dir=str(tmp_path), **_eval_kwargs(direction="long", p_stay=0.95, kelly_raw=0.5)
    )
    rec = _by_variant(records)["short_only"]
    assert rec["shadow_side"] == "blocked_by_direction"
    assert rec["would_trade"] is False
    assert rec["shadow_skip_reason"] == "direction_blocked: short_only"
