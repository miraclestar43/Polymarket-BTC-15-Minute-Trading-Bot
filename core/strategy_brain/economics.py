"""
Economic accounting for binary prediction-market settlements — DRY-RUN /
DIAGNOSTIC ONLY. This module NEVER places, sizes, or submits real orders and
does NOT influence any trading decision. It exists so hypothetical strategy
comparisons are economically valid and resistant to low-price payout artifacts.

--------------------------------------------------------------------------
Binary payout model (a YES/NO or UP/DOWN share settles at $1 or $0)
--------------------------------------------------------------------------
You buy `shares` of the token for the chosen side at price ``entry`` (the
recorded ``entry_price`` is the price of the token actually taken for that
side, so the math is symmetric for LONG/YES and SHORT/NO — only the winning
outcome differs):

    cost = shares * entry            # paid up-front == capital at risk
    if the side wins:  payout = shares * $1  -> net = shares * (1 - entry)
    if the side loses: payout = 0            -> net = -shares * entry

Because a binary contract can only fall to $0, the up-front cost IS the
maximum loss. So "capital at risk", "notional cost", and "maximum loss"
coincide for a single binary long. (This is why a separate *fixed-cost*
notional view is NOT independently meaningful — see EQUAL_SHARES below.)

--------------------------------------------------------------------------
Sizing modes
--------------------------------------------------------------------------
RECORDED_STAKE  (historical semantic):
    stake (== max loss) is fixed (default $5, the value the settlement
    pipeline recorded).  shares = stake / entry.
    Depends on historical sizing semantics; label results accordingly.

FIXED_RISK  (recommended fair comparison):
    max loss R is fixed (default $1) for EVERY trade regardless of side or
    price.  shares = R / entry.  This makes total capital at risk = R * N,
    so returns are directly comparable across variants with different trade
    counts and price mixes.
        LONG/YES : shares = R / entry ; win net = R*(1-entry)/entry ; loss = -R
        SHORT/NO : shares = R / entry ; win net = R*(1-entry)/entry ; loss = -R
    (entry is the price of the taken side's token in both cases.)

EQUAL_SHARES  (fixed FACE notional; optional, NOT risk-normalized):
    shares = N is fixed (equal payout exposure).  Capital at risk = N*entry
    VARIES with price, so risk is UNEQUAL across trades. Provided only to show
    equal-payout-exposure accounting; never rank strategies by it.

--------------------------------------------------------------------------
Why raw summed P/L is misleading (the ~$161k artifact)
--------------------------------------------------------------------------
Under any fixed-max-loss mode, a win at entry=0.01 returns ~99x the risk,
while most trades return a fraction of it. A handful of very-low-price winners
therefore dominate the *sum* even though the *median* trade is tiny. Ranking
by raw total P/L overweights these longshots. This module reports return on
capital at risk, medians, top-N concentration, and low-price sensitivity so
that longshot dependence is visible and never hidden.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.strategy_brain.settlement import outcome_wins_side

# Sizing modes.
RECORDED_STAKE = "recorded_stake"
FIXED_RISK = "fixed_risk"
EQUAL_SHARES = "equal_shares"

DEFAULT_RECORDED_STAKE = 5.0  # historical settlement stake (== max loss)
DEFAULT_FIXED_RISK = 1.0      # $1 max loss per trade for fair comparison
DEFAULT_EQUAL_SHARES = 1.0    # 1 contract per trade (face notional)

# Hypothetical entry-price floors for sensitivity analysis (analysis ONLY —
# never applied to the live strategy).
DEFAULT_PRICE_FLOORS: Tuple[float, ...] = (0.0, 0.02, 0.05, 0.10, 0.20)

# Low-price concentration buckets.
LOW_PRICE_BUCKETS: Tuple[float, ...] = (0.05, 0.10, 0.20)


# ---------------------------------------------------------------------------
# Price validation / safeguards
# ---------------------------------------------------------------------------
def validate_price(entry: Any) -> Tuple[bool, Optional[str]]:
    """
    A binary entry price must be strictly inside (0, 1). Returns (ok, reason).
    Prices <= 0 or >= 1 are impossible and are rejected (never silently used).
    """
    if entry is None:
        return False, "missing_price"
    try:
        p = float(entry)
    except (TypeError, ValueError):
        return False, "non_numeric_price"
    if p != p:  # NaN
        return False, "nan_price"
    if p <= 0.0:
        return False, "price_le_0"
    if p >= 1.0:
        return False, "price_ge_1"
    return True, None


# ---------------------------------------------------------------------------
# Single-trade settlement under a chosen sizing mode
# ---------------------------------------------------------------------------
def settle_trade(
    side: str,
    entry: Any,
    outcome: Optional[str],
    *,
    sizing_mode: str = FIXED_RISK,
    risk: float = DEFAULT_FIXED_RISK,
    stake: float = DEFAULT_RECORDED_STAKE,
    shares: float = DEFAULT_EQUAL_SHARES,
) -> Dict[str, Any]:
    """
    Settle ONE hypothetical binary trade. Returns a fully-labelled dict:

        valid, reason, win, entry_price, num_shares,
        capital_at_risk, max_loss, gross_payout, net_pnl, return_on_risk

    ``valid`` is False (with a ``reason``) for impossible prices, undecidable
    outcomes, or zero/undefined risk — such records are explicit, never counted
    as silent zeros.
    """
    base = {
        "valid": False, "reason": None, "win": None, "entry_price": None,
        "num_shares": None, "capital_at_risk": None, "max_loss": None,
        "gross_payout": None, "net_pnl": None, "return_on_risk": None,
        "sizing_mode": sizing_mode,
    }

    win = outcome_wins_side(outcome, side)
    if win is None:
        base["reason"] = "undecidable_outcome"
        return base

    ok, why = validate_price(entry)
    if not ok:
        base["reason"] = why
        base["win"] = win
        return base
    p = float(entry)

    if sizing_mode == RECORDED_STAKE:
        if stake <= 0:
            base["reason"] = "non_positive_stake"
            return base
        n_shares = stake / p
    elif sizing_mode == FIXED_RISK:
        if risk <= 0:
            base["reason"] = "non_positive_risk"
            return base
        n_shares = risk / p
    elif sizing_mode == EQUAL_SHARES:
        if shares <= 0:
            base["reason"] = "non_positive_shares"
            return base
        n_shares = float(shares)
    else:
        base["reason"] = f"unknown_sizing_mode:{sizing_mode}"
        return base

    capital_at_risk = n_shares * p  # == max loss for a binary long
    if capital_at_risk <= 0:
        base["reason"] = "zero_or_undefined_risk"
        return base

    gross_payout = n_shares if win else 0.0
    net_pnl = gross_payout - capital_at_risk
    base.update({
        "valid": True,
        "win": win,
        "entry_price": p,
        "num_shares": round(n_shares, 8),
        "capital_at_risk": round(capital_at_risk, 8),
        "max_loss": round(capital_at_risk, 8),
        "gross_payout": round(gross_payout, 8),
        "net_pnl": round(net_pnl, 8),
        "return_on_risk": round(net_pnl / capital_at_risk, 8),
    })
    return base


# ---------------------------------------------------------------------------
# Small stats helpers (deterministic, no numpy)
# ---------------------------------------------------------------------------
def _median(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def safe_profit_factor(pnls: Sequence[float]) -> Dict[str, Any]:
    """
    Profit factor = gross gains / gross losses. Returns an explicit dict so
    callers never emit a misleading infinite value.
    """
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses <= 0:
        return {"value": None, "note": "undefined_no_losing_trades",
                "gross_gain": round(gains, 6), "gross_loss": 0.0}
    return {"value": round(gains / losses, 4), "note": None,
            "gross_gain": round(gains, 6), "gross_loss": round(losses, 6)}


def max_drawdown(ordered_pnls: Sequence[float]) -> float:
    """Peak-to-trough drawdown of the cumulative net-P/L path (>= 0)."""
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for p in ordered_pnls:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    return round(mdd, 6)


def top_n_concentration(pnls: Sequence[float], ns: Sequence[int] = (1, 5, 10)) -> Dict[str, Any]:
    """
    Fraction of total NET P/L contributed by the top-N most profitable trades.
    Reported alongside the raw total so a dominant-longshot signature is
    visible. When total <= 0 the fraction is undefined (returned as None).
    """
    total = sum(pnls)
    ordered = sorted(pnls, reverse=True)
    out: Dict[str, Any] = {"total_pnl": round(total, 6), "n_trades": len(pnls)}
    for n in ns:
        top_sum = sum(ordered[:n])
        out[f"top_{n}_pnl"] = round(top_sum, 6)
        out[f"top_{n}_share"] = (round(top_sum / total, 4) if total > 0 else None)
    return out


def entry_price_distribution(entries: Sequence[float]) -> Dict[str, Any]:
    """Summary + low-price concentration counts of the entry-price mix."""
    if not entries:
        return {"n": 0, "min": None, "median": None, "max": None, "buckets": {}}
    buckets = {f"lt_{b}": sum(1 for e in entries if e < b) for b in LOW_PRICE_BUCKETS}
    return {
        "n": len(entries),
        "min": round(min(entries), 4),
        "median": round(_median(entries), 4),
        "max": round(max(entries), 4),
        "buckets": buckets,
    }


# ---------------------------------------------------------------------------
# Duplicate detection (one settlement contribution per decision)
# ---------------------------------------------------------------------------
def detect_duplicates(keys: Sequence[Any]) -> Dict[Any, int]:
    """Return {key: count} for keys appearing more than once."""
    counts: Dict[Any, int] = {}
    for k in keys:
        counts[k] = counts.get(k, 0) + 1
    return {k: c for k, c in counts.items() if c > 1}


def dedupe_trades(
    trades: Sequence[Dict[str, Any]], key_field: str = "stable_key"
) -> Tuple[List[Dict[str, Any]], Dict[Any, int]]:
    """
    Keep the FIRST occurrence per key so a market/variant decision contributes
    at most one settlement. Returns (deduped, duplicates_dropped).
    """
    seen: set = set()
    out: List[Dict[str, Any]] = []
    dropped: Dict[Any, int] = {}
    for t in trades:
        k = t.get(key_field)
        if k in seen:
            dropped[k] = dropped.get(k, 0) + 1
            continue
        seen.add(k)
        out.append(t)
    return out, dropped


# ---------------------------------------------------------------------------
# Side / variant aggregation
# ---------------------------------------------------------------------------
def aggregate(
    trades: Sequence[Dict[str, Any]],
    *,
    sizing_mode: str = FIXED_RISK,
    risk: float = DEFAULT_FIXED_RISK,
    stake: float = DEFAULT_RECORDED_STAKE,
    shares: float = DEFAULT_EQUAL_SHARES,
) -> Dict[str, Any]:
    """
    Aggregate a list of trade dicts (each with side/entry_price/outcome and,
    for ordering, an optional ``ts``) under one sizing mode.

    Returns resolved counts, total capital at risk, gross payout, net P/L,
    return on capital at risk, avg/median return per trade, max loss/gain,
    max drawdown, exposure-weighted win rate, entry-price distribution,
    low-price concentration, and top-1/5/10 concentration. Invalid records are
    counted separately and never treated as silent zeros.
    """
    ordered = sorted(trades, key=lambda t: t.get("ts") or 0)
    settled = []
    invalid: Dict[str, int] = {}
    for t in ordered:
        s = settle_trade(
            t.get("side", t.get("shadow_side", "unknown")),
            t.get("entry_price"), t.get("outcome"),
            sizing_mode=sizing_mode, risk=risk, stake=stake, shares=shares,
        )
        if s["valid"]:
            settled.append(s)
        else:
            invalid[s["reason"] or "invalid"] = invalid.get(s["reason"] or "invalid", 0) + 1

    n = len(settled)
    pnls = [s["net_pnl"] for s in settled]
    rors = [s["return_on_risk"] for s in settled]
    car = [s["capital_at_risk"] for s in settled]
    entries = [s["entry_price"] for s in settled]
    wins = [s for s in settled if s["win"]]

    total_car = sum(car)
    total_pnl = sum(pnls)
    win_risk = sum(s["capital_at_risk"] for s in wins)

    return {
        "sizing_mode": sizing_mode,
        "resolved_trades": n,
        "invalid_records": invalid,
        "wins": len(wins),
        "losses": n - len(wins),
        "win_rate_pct": round(len(wins) / n * 100, 2) if n else 0.0,
        "exposure_weighted_win_rate_pct": (
            round(win_risk / total_car * 100, 2) if total_car > 0 else 0.0),
        "total_capital_at_risk": round(total_car, 6),
        "gross_payout": round(sum(s["gross_payout"] for s in settled), 6),
        "net_pnl": round(total_pnl, 6),
        "return_on_capital_at_risk_pct": (
            round(total_pnl / total_car * 100, 3) if total_car > 0 else None),
        "avg_return_per_trade": round(sum(rors) / n, 6) if n else 0.0,
        "median_return_per_trade": round(_median(rors), 6) if n else 0.0,
        "avg_pnl_per_trade": round(total_pnl / n, 6) if n else 0.0,
        "median_pnl_per_trade": round(_median(pnls), 6) if n else 0.0,
        "max_loss": round(min(pnls), 6) if pnls else 0.0,
        "max_gain": round(max(pnls), 6) if pnls else 0.0,
        "max_drawdown": max_drawdown(pnls),
        "profit_factor": safe_profit_factor(pnls),
        "entry_price_distribution": entry_price_distribution(entries),
        "concentration": top_n_concentration(pnls),
    }


# ---------------------------------------------------------------------------
# Price-floor sensitivity (analysis only — NEVER applied to the strategy)
# ---------------------------------------------------------------------------
def price_floor_sensitivity(
    trades: Sequence[Dict[str, Any]],
    floors: Sequence[float] = DEFAULT_PRICE_FLOORS,
    *,
    sizing_mode: str = FIXED_RISK,
    risk: float = DEFAULT_FIXED_RISK,
) -> List[Dict[str, Any]]:
    """
    Show how much of a result depends on very low-priced contracts by excluding
    trades with entry < floor. This is a SENSITIVITY table only; it is never a
    rule imposed on the bot.
    """
    rows = []
    for floor in floors:
        kept = [t for t in trades
                if (validate_price(t.get("entry_price"))[0]
                    and float(t["entry_price"]) >= floor)]
        agg = aggregate(kept, sizing_mode=sizing_mode, risk=risk)
        rows.append({
            "price_floor": floor,
            "resolved_trades": agg["resolved_trades"],
            "net_pnl": agg["net_pnl"],
            "return_on_capital_at_risk_pct": agg["return_on_capital_at_risk_pct"],
            "median_return_per_trade": agg["median_return_per_trade"],
            "win_rate_pct": agg["win_rate_pct"],
        })
    return rows
