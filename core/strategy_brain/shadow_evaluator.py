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

from core.strategy_brain import paper_execution
from core.diagnostics import run_provenance


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

# Schema version for shadow decision records. v1 = original (entry_price is the
# YES-token midpoint, no executable quote, no token ids). v2 adds an explicit,
# self-describing executable-quote block so future records are auditable without
# guessing which token a price refers to. Bumping this NEVER changes signal or
# selection logic.
SHADOW_SCHEMA_VERSION = 2


def _run_provenance_fields() -> Dict[str, Any]:
    """Run-provenance fields merged into every shadow record: run_id,
    git_head, git_branch, git_dirty, strategy_config_sha256. Reads the
    already-computed values cached by run_provenance.get_current_run_provenance()
    (env vars set once by the runner) — no Git call, no file hash here."""
    p = run_provenance.get_current_run_provenance()
    return {
        "run_id": p["run_id"],
        "git_head": p["git_head"],
        "git_branch": p["git_branch"],
        "git_dirty": p["git_dirty"],
        "strategy_config_sha256": p["strategy_config_sha256"],
    }


def _forward_schema_fields(
    shadow_direction: str,
    yes_midpoint: Optional[float],
    quote: Optional[Dict[str, Any]],
    decision_timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build the v2 self-describing executable-quote block for one record.

    Keeps the concepts strictly separate: YES-token quote, NO-token quote,
    selected-token executable entry price, paper-execution (VWAP) fill, and
    (elsewhere) the model / market probabilities and official settlement. A YES
    price is never reused as a NO price. When the executable price of the token
    actually taken cannot be captured (e.g. the NO book was missing, stale,
    crossed, or invalid) the record is explicitly marked
    ``economic_pnl_unavailable`` with a reason instead of substituting a derived
    or model value.
    """
    q = quote or {}
    yes_bid = q.get("yes_bid")
    yes_ask = q.get("yes_ask")
    # YES mid: prefer an explicit executable mid, else the recorded midpoint.
    if yes_bid is not None and yes_ask is not None:
        yes_mid = (float(yes_bid) + float(yes_ask)) / 2.0
    else:
        yes_mid = yes_midpoint
    no_bid = q.get("no_bid")
    no_ask = q.get("no_ask")
    no_quote_captured = no_bid is not None and no_ask is not None
    if no_quote_captured:
        no_mid: Optional[float] = (float(no_bid) + float(no_ask)) / 2.0
        no_mid_source = "no_book"
    elif yes_mid is not None:
        no_mid = 1.0 - float(yes_mid)
        no_mid_source = "derived_from_yes_mid"  # NOT executable
    else:
        no_mid = None
        no_mid_source = "unavailable"

    yes_token_id = q.get("yes_token_id")
    no_token_id = q.get("no_token_id")

    if shadow_direction == "long":
        selected_token_id = yes_token_id
        selected_exec_price = float(yes_ask) if yes_ask is not None else None
        exec_source = "yes_ask" if yes_ask is not None else "unavailable"
        selected_ask_size = q.get("yes_ask_size")
        selected_ask_ladder = q.get("yes_ask_ladder")
        selected_quote_ts = q.get("yes_quote_timestamp")
        selected_quote_age = q.get("yes_quote_age_s")
    elif shadow_direction == "short":
        selected_token_id = no_token_id
        selected_exec_price = float(no_ask) if no_ask is not None else None
        exec_source = "no_ask" if no_ask is not None else "unavailable"
        selected_ask_size = q.get("no_ask_size")
        selected_ask_ladder = q.get("no_ask_ladder")
        selected_quote_ts = q.get("no_quote_timestamp")
        selected_quote_age = q.get("no_quote_age_s")
    else:
        selected_token_id = None
        selected_exec_price = None
        exec_source = "n/a_no_trade"
        selected_ask_size = None
        selected_ask_ladder = None
        selected_quote_ts = None
        selected_quote_age = None

    economic_pnl_unavailable = selected_exec_price is None
    if economic_pnl_unavailable:
        if shadow_direction == "short" and not no_quote_captured:
            reason = "no_token_ask_not_captured"
        elif shadow_direction == "long" and yes_ask is None:
            reason = "yes_token_ask_not_captured"
        elif shadow_direction == "blocked_by_direction":
            reason = "no_trade"
        else:
            reason = "executable_quote_unavailable"
    else:
        reason = None

    # -- paper-execution (VWAP) fill on the selected token's ask depth -------
    # Uses the contemporaneous ask ladder when present; falls back to a single
    # observed level (best ask + its size) — never fabricates deeper liquidity.
    ladder = selected_ask_ladder
    if not ladder and selected_exec_price is not None and selected_ask_size:
        ladder = [(selected_exec_price, selected_ask_size)]
    requested_stake = q.get("paper_fixed_risk", paper_execution.DEFAULT_FIXED_RISK)
    if selected_exec_price is None:
        paper = paper_execution.simulate_fill(None, requested_stake=requested_stake)
    else:
        paper = paper_execution.simulate_fill(ladder, requested_stake=requested_stake)
    executable_economic_available = (
        not economic_pnl_unavailable
        and paper["fill_status"] in (paper_execution.FILLED,
                                     paper_execution.PARTIALLY_FILLED))

    # -- side-level validation flags (merged with book-level flags) ---------
    flags = dict(q.get("validation_flags") or {})
    flags["selected_token_matches_side"] = (
        shadow_direction not in ("long", "short")
        or selected_token_id == (yes_token_id if shadow_direction == "long"
                                 else no_token_id))
    quote_before_or_at_decision = True
    if decision_timestamp is not None and selected_quote_ts is not None:
        try:
            from datetime import datetime as _dt
            dec = _dt.fromisoformat(str(decision_timestamp).replace("Z", "+00:00"))
            dec_epoch = dec.timestamp()
            # allow 1s tolerance for same-instant record ordering
            quote_before_or_at_decision = float(selected_quote_ts) <= dec_epoch + 1.0
        except (ValueError, TypeError):
            quote_before_or_at_decision = True
    flags["quote_before_or_at_decision"] = quote_before_or_at_decision

    return {
        "schema_version": SHADOW_SCHEMA_VERSION,
        "condition_id": q.get("condition_id"),
        "market_id_v2": q.get("market_id"),
        "market_start_time": q.get("market_start_time"),
        "market_end_time": q.get("market_end_time"),
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        "selected_token_id": selected_token_id,
        # YES book
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "yes_mid": yes_mid,
        "yes_bid_size": q.get("yes_bid_size"),
        "yes_ask_size": q.get("yes_ask_size"),
        "yes_quote_timestamp": q.get("yes_quote_timestamp"),
        "yes_quote_age_s": q.get("yes_quote_age_s"),
        "yes_book_status": q.get("yes_book_status"),
        # NO book
        "no_bid": no_bid,
        "no_ask": no_ask,
        "no_mid": no_mid,
        "no_mid_source": no_mid_source,
        "no_bid_size": q.get("no_bid_size"),
        "no_ask_size": q.get("no_ask_size"),
        "no_quote_timestamp": q.get("no_quote_timestamp"),
        "no_quote_age_s": q.get("no_quote_age_s"),
        "no_book_status": q.get("no_book_status"),
        # cross-book + provenance
        "cross_book_skew_s": q.get("cross_book_skew_s"),
        "book_generation": q.get("book_generation"),
        "quote_timestamp": q.get("quote_timestamp"),
        "quote_source": q.get("quote_source"),
        # selected-token executable entry
        "selected_exec_price": selected_exec_price,
        "selected_exec_price_source": exec_source,
        "selected_token_ask_size": selected_ask_size,
        "selected_quote_age_s": selected_quote_age,
        "economic_pnl_unavailable": economic_pnl_unavailable,
        "economic_pnl_unavailable_reason": reason,
        # paper-execution (diagnostic VWAP fill; separate from strategy sizing)
        "paper_requested_stake": paper.get("requested_stake"),
        "paper_requested_shares": paper.get("requested_shares"),
        "paper_fill_status": paper.get("fill_status"),
        "paper_filled_shares": paper.get("filled_shares"),
        "paper_spent": paper.get("spent"),
        "paper_vwap": paper.get("vwap"),
        "paper_unfilled_stake": paper.get("unfilled_stake"),
        "paper_top_ask": paper.get("top_ask"),
        "paper_top_ask_size": paper.get("top_ask_size"),
        "paper_ask_levels_used": paper.get("ask_levels_used"),
        "paper_slippage_model": paper.get("slippage_model"),
        "paper_fee_rate": paper.get("fee_rate"),
        "paper_fee_source": paper.get("fee_source"),
        "paper_pre_fee": paper.get("pre_fee"),
        "executable_economic_available": executable_economic_available,
        # validation
        "validation_flags": flags,
    }


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
        executable_quote: Optional[Dict[str, Any]] = None,
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
                executable_quote=executable_quote,
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

        # Final decision. `has_valid_side` is required in addition to the
        # per-gate checks below: the skip_kelly / kelly_min_raw relaxed paths
        # above only verify `shadow_direction != "blocked_by_direction"`, so
        # when the REAL signal was neutral (direction="unknown", passed in by
        # bot.py when neither BULLISH nor BEARISH), a stale/persisted Markov
        # p_stay from a PRIOR cycle could still satisfy markov_passed_shadow
        # and (with Kelly relaxed/skipped) kelly_passed_shadow, flagging
        # would_trade=True with no side to trade (selected_token_id stays
        # null downstream). Proven in the 2026-07-20..27 collection run: 48
        # such records, confined to shadow_no_kelly / shadow_allow_kelly_*
        # / relaxed_markov_and_kelly_shadow. This gate closes that forward
        # only; it does not reinterpret or rewrite any historical record.
        has_valid_side = shadow_direction in ("long", "short")
        would_trade = (
            has_valid_side
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
            elif not has_valid_side:
                shadow_skip_reason = f"no_valid_side: actual_direction={actual_direction}"
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

        forward = _forward_schema_fields(
            shadow_direction, kwargs.get("entry_price"),
            kwargs.get("executable_quote"),
            decision_timestamp=kwargs.get("timestamp"),
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
            # Run provenance (computed once by the runner at startup, read
            # here from env vars set for this child — never a Git call or a
            # file hash per record). Lets a future audit prove which code
            # version produced this record instead of inferring it from
            # wall-clock time, per the 2026-07-20..27 closeout finding.
            **_run_provenance_fields(),
            **forward,
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
    executable_quote: Optional[Dict[str, Any]] = None,
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
        executable_quote=executable_quote,
    )
