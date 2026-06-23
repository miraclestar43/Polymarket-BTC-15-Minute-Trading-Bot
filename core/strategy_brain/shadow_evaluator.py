"""
Shadow Evaluation Mode

Logs hypothetical decisions under alternative parameter sets for
EVERY candidate decision/evaluation cycle — including skipped trades.
Writes to logs/shadow_decisions_YYYY-MM-DD.jsonl.

Shadow configs tested:
  1. Current baseline
  2. min_prob = 0.82
  3. min_prob = 0.84
  4. markov_window = 10
  5. markov_window = 15
  6. SHORT-only mode
  7. LONG-only mode
  8-10. Relaxed Kelly variants
  11-15. Regime-conditioned variants (block_long_macro_bear, short_bias_macro_bear,
         side_specific_threshold, trend_guard, relaxed_markov_and_kelly)

No simulated orders, no Telegram alerts, no paper_trades.json impact.
All records are DRY-RUN / SHADOW only (dry_run=True, simulation=True).
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any


# Built-in shadow configuration variants. These are diagnostic-only and never
# affect live/baseline execution. They can be extended (not replaced) by a
# dry-run-only config section via load_shadow_configs().
SHADOW_CONFIGS = [
    {"name": "baseline", "description": "Current parameters"},
    {"name": "min_prob_082", "description": "min_prob=0.82", "min_prob": 0.82},
    {"name": "min_prob_084", "description": "min_prob=0.84", "min_prob": 0.84},
    {"name": "markov_window_10", "description": "markov_window=10", "markov_window": 10},
    {"name": "markov_window_15", "description": "markov_window=15", "markov_window": 15},
    {"name": "short_only", "description": "SHORT trades only", "allow_long": False},
    {"name": "long_only", "description": "LONG trades only", "allow_short": False},
    # Shadow-only relaxed Kelly configs (diagnostic only, no simulated orders)
    # "allow_kelly_minus_X" means: allow trade if raw_kelly_fraction > -X
    # This lets us see how many candidates would appear with relaxed Kelly
    {"name": "shadow_no_kelly", "description": "No Kelly filter", "skip_kelly": True},
    {"name": "shadow_allow_kelly_minus_002", "description": "Allow Kelly down to -0.02", "kelly_min_raw": -0.02},
    {"name": "shadow_allow_kelly_minus_005", "description": "Allow Kelly down to -0.05", "kelly_min_raw": -0.05},
    # --- Regime-conditioned shadow variants (diagnostic only) ---
    # block_long_macro_bear: skip LONG entries when macro regime is bearish.
    {"name": "block_long_macro_bear_shadow",
     "description": "Block LONG when macro_regime=bear",
     "block_long_macro_bear": True},
    # short_bias_macro_bear: in macro_bear, allow only SHORT (block LONG); elsewhere unchanged.
    {"name": "short_bias_macro_bear_shadow",
     "description": "Prefer SHORT in macro_bear (block LONG in bear)",
     "short_bias_macro_bear": True},
    # side_specific_threshold: different min_prob for LONG vs SHORT.
    {"name": "side_specific_threshold_shadow",
     "description": "min_prob long=0.88 / short=0.84",
     "min_prob_long": 0.88, "min_prob_short": 0.84},
    # trend_guard: widen the neutral price band where no trade is taken
    # (baseline trades only when price>0.60 long / <0.40 short; here require a
    # stronger edge: price>0.65 long / <0.35 short).
    {"name": "trend_guard_shadow",
     "description": "Stricter trend band (0.35/0.65)",
     "trend_long_min": 0.65, "trend_short_max": 0.35},
    # relaxed_markov_and_kelly: relax BOTH gates together.
    {"name": "relaxed_markov_and_kelly_shadow",
     "description": "min_prob=0.82 AND allow Kelly down to -0.02",
     "min_prob": 0.82, "kelly_min_raw": -0.02},
]

# Optional dry-run-only config file. If present, its `extra_variants` list is
# APPENDED to the built-ins (never replaces baseline behavior).
_SHADOW_CONFIG_FILE = "shadow_experiments.yaml"


def load_shadow_configs(config_path: str = _SHADOW_CONFIG_FILE) -> List[Dict[str, Any]]:
    """
    Return the active shadow variant list: built-in SHADOW_CONFIGS plus any
    `extra_variants` defined in an optional dry-run-only YAML file. This never
    alters baseline/live execution — it only adds diagnostic shadow variants.
    """
    configs = list(SHADOW_CONFIGS)
    try:
        p = Path(config_path)
        if p.exists():
            import yaml  # local import: optional dependency path
            with open(p) as f:
                data = yaml.safe_load(f) or {}
            extra = data.get("extra_variants") or []
            seen = {c["name"] for c in configs}
            for v in extra:
                if isinstance(v, dict) and v.get("name") and v["name"] not in seen:
                    configs.append(v)
                    seen.add(v["name"])
    except Exception:
        # Fail safe: never let a bad config file break shadow logging.
        return list(SHADOW_CONFIGS)
    return configs



class ShadowEvaluator:
    """
    Evaluates hypothetical decisions under alternative parameter sets.
    Runs for EVERY decision cycle — authorized trades AND skipped trades.
    """

    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)

    @staticmethod
    def _required_p_model(odds_b: float) -> Optional[float]:
        """Calculate minimum p_model needed for Kelly raw > 0.
        Kelly: raw = p - (1-p)/b > 0  =>  p > 1/(b+1)
        """
        if odds_b <= 0:
            return None
        return round(1.0 / (odds_b + 1.0), 6)

    @staticmethod
    def _kelly_gap(p_model: float, odds_b: float) -> Optional[float]:
        """Calculate gap = p_model - required_p_model.
        Positive = would pass Kelly, Negative = would fail.
        """
        if odds_b <= 0 or p_model <= 0:
            return None
        required = 1.0 / (odds_b + 1.0)
        return round(p_model - required, 6)

    def evaluate(
        self,
        actual_event_type: str,
        actual_decision_reason: str,
        timestamp: str,
        market_id: str,
        direction: str,
        entry_price: float,
        p_model: float,
        p_model_source: str,
        odds_b: float,
        kelly_raw: float,
        kelly_capped: float,
        kelly_size: float,
        markov_state: str,
        p_stay: float,
        min_prob_actual: float,
        markov_window_actual: int,
        markov_passed: bool,
        kelly_passed: bool,
        liquidity_passed: Optional[bool] = None,
        fee_estimate: Optional[float] = None,
        fee_source: Optional[str] = None,
        btc_price: Optional[float] = None,
        btc_return_15m: Optional[float] = None,
        btc_return_1h: Optional[float] = None,
        btc_return_4h: Optional[float] = None,
        btc_regime: Optional[str] = None,
        regime_source: Optional[str] = None,
        regime_timestamp: Optional[str] = None,
        resolution_status: Optional[str] = None,
        resolved_outcome: Optional[str] = None,
        resolution_time: Optional[str] = None,
        resolution_source: Optional[str] = None,
        resolution_error: Optional[str] = None,
        market_slug: Optional[str] = None,
        market_question: Optional[str] = None,
        market_end_time: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Evaluate all shadow configs and write results.
        Called at EVERY decision point — authorized trades and skipped trades.
        """
        records = []
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_path = self.log_dir / f"shadow_decisions_{today}.jsonl"

        for config in load_shadow_configs():
            record = self._evaluate_single(
                config=config,
                actual_event_type=actual_event_type,
                actual_decision_reason=actual_decision_reason,
                timestamp=timestamp,
                market_id=market_id,
                direction=direction,
                entry_price=entry_price,
                p_model=p_model,
                p_model_source=p_model_source,
                odds_b=odds_b,
                kelly_raw=kelly_raw,
                kelly_capped=kelly_capped,
                kelly_size=kelly_size,
                markov_state=markov_state,
                p_stay=p_stay,
                min_prob_actual=min_prob_actual,
                markov_window_actual=markov_window_actual,
                markov_passed=markov_passed,
                kelly_passed=kelly_passed,
                liquidity_passed=liquidity_passed,
                fee_estimate=fee_estimate,
                fee_source=fee_source,
                btc_price=btc_price,
                btc_return_15m=btc_return_15m,
                btc_return_1h=btc_return_1h,
                btc_return_4h=btc_return_4h,
                btc_regime=btc_regime,
                regime_source=regime_source,
                regime_timestamp=regime_timestamp,
                resolution_status=resolution_status,
                resolved_outcome=resolved_outcome,
                resolution_time=resolution_time,
                resolution_source=resolution_source,
                resolution_error=resolution_error,
                market_slug=market_slug,
                market_question=market_question,
                market_end_time=market_end_time,
            )
            records.append(record)

        # Write all records
        try:
            with open(log_path, "a") as f:
                for record in records:
                    f.write(json.dumps(record) + "\n")
        except Exception:
            pass

        return records

    def _evaluate_single(
        self,
        config: Dict[str, Any],
        **kwargs,
    ) -> Dict[str, Any]:
        """Evaluate a single shadow config."""
        name = config["name"]

        # Apply config overrides
        min_prob = config.get("min_prob", kwargs["min_prob_actual"])
        allow_long = config.get("allow_long", True)
        allow_short = config.get("allow_short", True)
        skip_kelly = config.get("skip_kelly", False)
        kelly_min_raw = config.get("kelly_min_raw", None)

        # Regime-conditioned / side-specific / trend-guard variant params
        block_long_macro_bear = config.get("block_long_macro_bear", False)
        short_bias_macro_bear = config.get("short_bias_macro_bear", False)
        min_prob_long = config.get("min_prob_long")
        min_prob_short = config.get("min_prob_short")
        trend_long_min = config.get("trend_long_min")
        trend_short_max = config.get("trend_short_max")

        # Derive macro regime from the combined regime string ("macro|intraday").
        btc_regime_str = kwargs.get("btc_regime") or ""
        macro_regime = btc_regime_str.split("|")[0].lower() if btc_regime_str else ""
        is_macro_bear = "bear" in macro_regime

        # Direction filter
        actual_direction = kwargs["direction"]
        if name == "short_only" and actual_direction == "long":
            shadow_direction = "blocked_by_direction"
        elif name == "long_only" and actual_direction == "short":
            shadow_direction = "blocked_by_direction"
        elif (block_long_macro_bear or short_bias_macro_bear) and \
                actual_direction == "long" and is_macro_bear:
            # Block LONG entries while macro regime is bearish.
            shadow_direction = "blocked_by_direction"
        else:
            shadow_direction = actual_direction

        # Side-specific min_prob (overrides the single min_prob when set).
        if shadow_direction == "long" and min_prob_long is not None:
            min_prob = min_prob_long
        elif shadow_direction == "short" and min_prob_short is not None:
            min_prob = min_prob_short

        # Trend-guard: require a stronger price edge than baseline. If the entry
        # price is inside the (wider) neutral band, treat as no-trade.
        trend_guard_block = False
        if shadow_direction != "blocked_by_direction" and \
                (trend_long_min is not None or trend_short_max is not None):
            entry = kwargs.get("entry_price")
            if entry is not None:
                if shadow_direction == "long" and trend_long_min is not None \
                        and entry < trend_long_min:
                    trend_guard_block = True
                elif shadow_direction == "short" and trend_short_max is not None \
                        and entry > trend_short_max:
                    trend_guard_block = True

        # Markov check
        markov_passed_shadow = (
            kwargs["p_stay"] >= min_prob
            if shadow_direction != "blocked_by_direction"
            else False
        )

        # Kelly check (with optional relaxation for shadow-only configs)
        kelly_raw_val = kwargs.get("kelly_raw", 0.0)
        if skip_kelly:
            # shadow_no_kelly: skip Kelly check entirely
            kelly_passed_shadow = markov_passed_shadow and shadow_direction != "blocked_by_direction"
        elif kelly_min_raw is not None:
            # shadow_allow_kelly_minus_X: allow if raw_kelly_fraction > kelly_min_raw
            kelly_passed_shadow = (
                kelly_raw_val > kelly_min_raw
                and markov_passed_shadow
                and shadow_direction != "blocked_by_direction"
            )
        else:
            # Standard Kelly check
            kelly_passed_shadow = (
                markov_passed_shadow
                and shadow_direction != "blocked_by_direction"
                and kwargs["kelly_passed"]
            )

        # Liquidity (same as actual, or True if unknown)
        liquidity_passed = kwargs.get("liquidity_passed") if kwargs.get("liquidity_passed") is not None else True

        # Fee-aware EV (placeholder)
        fee_aware_ev = None
        fee_aware_passed = True
        if kwargs["fee_estimate"] is not None:
            fee_aware_ev = kwargs["kelly_raw"] - kwargs["fee_estimate"]
            fee_aware_passed = fee_aware_ev > 0

        # Final decision
        would_trade = (
            shadow_direction != "blocked_by_direction"
            and not trend_guard_block
            and markov_passed_shadow
            and kelly_passed_shadow
            and liquidity_passed
            and fee_aware_passed
        )

        # Skip reason
        shadow_skip_reason = None
        if not would_trade:
            if shadow_direction == "blocked_by_direction":
                shadow_skip_reason = f"direction_blocked: {name}"
            elif trend_guard_block:
                shadow_skip_reason = f"trend_guard_block: entry={kwargs.get('entry_price')}"
            elif not markov_passed_shadow:
                shadow_skip_reason = f"markov_below_threshold: p_stay={kwargs['p_stay']:.4f}<min_prob={min_prob:.4f}"
            elif not kelly_passed_shadow:
                shadow_skip_reason = "kelly_no_edge"
            elif not liquidity_passed:
                shadow_skip_reason = "liquidity_guard"
            elif not fee_aware_passed:
                shadow_skip_reason = "fee_aware_ev_negative"

        # Side label
        side_label = "BUY YES" if shadow_direction == "long" else (
            "BUY NO" if shadow_direction == "short" else "N/A"
        )

        return {
            "timestamp": kwargs["timestamp"],
            "market_id": kwargs["market_id"],
            "actual_event_type": kwargs["actual_event_type"],
            "actual_decision_reason": kwargs["actual_decision_reason"],
            "config_name": name,
            "config_description": config.get("description", ""),
            "shadow_mode": name,
            "actual_side": actual_direction,
            "shadow_side": shadow_direction,
            "side_label": side_label,
            "entry_price": kwargs["entry_price"],
            "p_model": kwargs["p_model"],
            "p_model_source": kwargs["p_model_source"],
            "p_stay": kwargs["p_stay"],
            "markov_state": kwargs["markov_state"],
            "min_prob_used": min_prob,
            "shadow_markov_window": config.get("markov_window"),
            "odds_b": kwargs["odds_b"],
            "raw_kelly_fraction": kwargs["kelly_raw"],
            "capped_kelly_fraction": kwargs["kelly_capped"],
            "kelly_size": kwargs["kelly_size"],
            "markov_passed": markov_passed_shadow,
            "kelly_passed": kelly_passed_shadow,
            "liquidity_passed": liquidity_passed,
            "fee_estimate": kwargs["fee_estimate"],
            "fee_source": kwargs["fee_source"],
            "fee_schedule_rate": None,
            "maker_base_fee": None,
            "taker_base_fee": None,
            "fee_aware_ev": fee_aware_ev,
            "btc_price": kwargs.get("btc_price"),
            "btc_return_15m": kwargs.get("btc_return_15m"),
            "btc_return_1h": kwargs.get("btc_return_1h"),
            "btc_return_4h": kwargs.get("btc_return_4h"),
            "btc_regime": kwargs.get("btc_regime", "unavailable"),
            "regime_source": kwargs.get("regime_source", "not_implemented"),
            "regime_timestamp": kwargs.get("regime_timestamp"),
            # Kelly bottleneck diagnostics
            "required_p_model_to_pass": self._required_p_model(kwargs.get("odds_b", 0.0)),
            "kelly_gap": self._kelly_gap(kwargs.get("p_model", 0.0), kwargs.get("odds_b", 0.0)),
            # Market resolution (for outcome matching)
            "resolution_status": kwargs.get("resolution_status", "pending"),
            "resolved_outcome": kwargs.get("resolved_outcome"),
            "resolution_time": kwargs.get("resolution_time"),
            "resolution_source": kwargs.get("resolution_source"),
            "resolution_error": kwargs.get("resolution_error"),
            # Market identifiers for resolution
            "market_slug": kwargs.get("market_slug"),
            "market_question": kwargs.get("market_question"),
            "market_end_time": kwargs.get("market_end_time"),
            "macro_regime": macro_regime or "unavailable",
            "trend_guard_block": trend_guard_block,
            "would_trade": would_trade,
            "shadow_skip_reason": shadow_skip_reason,
            "dry_run": True,
            "simulation": True,
        }


def evaluate_shadow(
    actual_event_type: str,
    actual_decision_reason: str,
    timestamp: str,
    market_id: str,
    direction: str,
    entry_price: float,
    p_model: float,
    p_model_source: str,
    odds_b: float,
    kelly_raw: float,
    kelly_capped: float,
    kelly_size: float,
    markov_state: str,
    p_stay: float,
    min_prob_actual: float,
    markov_window_actual: int,
    markov_passed: bool,
    kelly_passed: bool,
    liquidity_passed: Optional[bool] = None,
    fee_estimate: Optional[float] = None,
    fee_source: Optional[str] = None,
    btc_price: Optional[float] = None,
    btc_return_15m: Optional[float] = None,
    btc_return_1h: Optional[float] = None,
    btc_return_4h: Optional[float] = None,
    btc_regime: Optional[str] = None,
    regime_source: Optional[str] = None,
    regime_timestamp: Optional[str] = None,
    resolution_status: Optional[str] = None,
    resolved_outcome: Optional[str] = None,
    resolution_time: Optional[str] = None,
    resolution_source: Optional[str] = None,
    resolution_error: Optional[str] = None,
    market_slug: Optional[str] = None,
    market_question: Optional[str] = None,
    market_end_time: Optional[str] = None,
    log_dir: str = "logs",
) -> List[Dict[str, Any]]:
    """Convenience function to evaluate shadow configs."""
    evaluator = ShadowEvaluator(log_dir=log_dir)
    return evaluator.evaluate(
        actual_event_type=actual_event_type,
        actual_decision_reason=actual_decision_reason,
        timestamp=timestamp,
        market_id=market_id,
        direction=direction,
        entry_price=entry_price,
        p_model=p_model,
        p_model_source=p_model_source,
        odds_b=odds_b,
        kelly_raw=kelly_raw,
        kelly_capped=kelly_capped,
        kelly_size=kelly_size,
        markov_state=markov_state,
        p_stay=p_stay,
        min_prob_actual=min_prob_actual,
        markov_window_actual=markov_window_actual,
        markov_passed=markov_passed,
        kelly_passed=kelly_passed,
        liquidity_passed=liquidity_passed,
        fee_estimate=fee_estimate,
        fee_source=fee_source,
        btc_price=btc_price,
        btc_return_15m=btc_return_15m,
        btc_return_1h=btc_return_1h,
        btc_return_4h=btc_return_4h,
        btc_regime=btc_regime,
        regime_source=regime_source,
        regime_timestamp=regime_timestamp,
        resolution_status=resolution_status,
        resolved_outcome=resolved_outcome,
        resolution_time=resolution_time,
        resolution_source=resolution_source,
        resolution_error=resolution_error,
        market_slug=market_slug,
        market_question=market_question,
        market_end_time=market_end_time,
    )
