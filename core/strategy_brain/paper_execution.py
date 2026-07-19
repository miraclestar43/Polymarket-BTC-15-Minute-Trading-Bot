"""
Paper-execution / VWAP fill simulator (DRY-RUN / DIAGNOSTIC ONLY).

Separate from all strategy logic. Given the contemporaneous ask-side depth of
the SELECTED token and a fixed maximum-risk budget, it simulates walking the
book to estimate a realistic fill: filled shares, spent amount, VWAP, unfilled
amount and a fill status. It NEVER fabricates liquidity — it only consumes the
depth actually observed. If the observed depth cannot fill the configured
stake, it reports an explicitly labelled partial fill (default policy) or marks
the order unavailable.

Concepts are kept strictly separate from: signal/model probability, market
midpoint, best executable ask, and official settlement outcome.

This module makes no network calls, signs nothing, and submits nothing.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

Ladder = List[Tuple[float, float]]

FILLED = "filled"
PARTIALLY_FILLED = "partially_filled"
UNAVAILABLE = "unavailable"

# Default fixed maximum risk ($ max loss) per hypothetical order. This mirrors
# the report's fair-comparison convention and is a DIAGNOSTIC sizing budget for
# the paper-fill estimate only — it does NOT change the strategy's Kelly sizing.
DEFAULT_FIXED_RISK = 1.0

# Fill-shortfall policy when observed depth cannot absorb the full stake.
POLICY_PARTIAL = "partial"          # record an explicit partial fill
POLICY_UNAVAILABLE = "unavailable"  # mark the whole order unavailable

_EPS = 1e-9


def _clean_ladder(ladder: Optional[Ladder]) -> Ladder:
    """Keep only valid (0<price<1, size>0) levels, sorted best (lowest) ask first."""
    if not ladder:
        return []
    out = []
    for lvl in ladder:
        try:
            price, size = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0.0 < price < 1.0 and size > 0:
            out.append((price, size))
    out.sort(key=lambda x: x[0])
    return out


def simulate_fill(
    ask_ladder: Optional[Ladder],
    *,
    requested_stake: float = DEFAULT_FIXED_RISK,
    fee_rate: Optional[float] = None,
    fee_source: Optional[str] = None,
    partial_policy: str = POLICY_PARTIAL,
    outcome_win: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Simulate buying the selected token by walking ``ask_ladder`` (ascending
    price) up to ``requested_stake`` dollars of capital at risk.

    ``requested_stake`` is the fixed maximum risk (== max loss for a binary
    long, since a losing token settles to 0). For each level we spend
    price*size until the budget is exhausted or the book runs out.

    If ``outcome_win`` is provided the settled economics are also returned;
    otherwise only the pre-settlement fill is described (P/L computed later at
    settlement time).

    Fee handling: fees are applied ONLY when a reliable ``fee_rate`` is given.
    When it is None the result is labelled pre-fee (``fee_source='pre_fee'``)
    and no fee is invented.
    """
    ladder = _clean_ladder(ask_ladder)
    top_ask = ladder[0][0] if ladder else None
    top_ask_size = ladder[0][1] if ladder else None

    base: Dict[str, Any] = {
        "requested_stake": round(float(requested_stake), 8),
        "requested_shares": None,
        "top_ask": top_ask,
        "top_ask_size": top_ask_size,
        "ask_levels_used": [],
        "filled_shares": 0.0,
        "spent": 0.0,
        "vwap": None,
        "unfilled_stake": round(float(requested_stake), 8),
        "fill_status": UNAVAILABLE,
        "slippage_model": "walk_observed_ask_depth",
        "fee_source": fee_source or ("pre_fee" if fee_rate is None else "provided"),
        "fee_rate": fee_rate,
        "pre_fee": fee_rate is None,
        # settled economics (only when outcome_win is provided AND filled)
        "win": outcome_win,
        "gross_payout": None,
        "fees": None,
        "net_pnl": None,
        "max_loss": None,
        "return_on_risk": None,
    }

    if not ladder or requested_stake <= 0:
        base["requested_shares"] = (round(requested_stake / top_ask, 8)
                                    if top_ask else None)
        return base

    base["requested_shares"] = round(requested_stake / top_ask, 8)

    remaining = float(requested_stake)
    filled_shares = 0.0
    spent = 0.0
    levels_used: List[Tuple[float, float]] = []
    for price, size in ladder:
        if remaining <= _EPS:
            break
        cost_full = price * size
        if cost_full <= remaining + _EPS:
            filled_shares += size
            spent += cost_full
            remaining -= cost_full
            levels_used.append((round(price, 8), round(size, 8)))
        else:
            shares = remaining / price
            filled_shares += shares
            spent += shares * price
            levels_used.append((round(price, 8), round(shares, 8)))
            remaining = 0.0
            break

    unfilled = max(0.0, float(requested_stake) - spent)
    if spent <= _EPS:
        status = UNAVAILABLE
    elif unfilled <= _EPS:
        status = FILLED
    else:
        status = PARTIALLY_FILLED

    # Apply the shortfall policy.
    if status == PARTIALLY_FILLED and partial_policy == POLICY_UNAVAILABLE:
        status = UNAVAILABLE
        base.update({
            "fill_status": UNAVAILABLE,
            "ask_levels_used": levels_used,
            "filled_shares": round(filled_shares, 8),
            "spent": round(spent, 8),
            "vwap": round(spent / filled_shares, 8) if filled_shares > 0 else None,
            "unfilled_stake": round(unfilled, 8),
        })
        return base

    vwap = spent / filled_shares if filled_shares > 0 else None
    base.update({
        "fill_status": status,
        "ask_levels_used": levels_used,
        "filled_shares": round(filled_shares, 8),
        "spent": round(spent, 8),
        "vwap": round(vwap, 8) if vwap is not None else None,
        "unfilled_stake": round(unfilled, 8),
    })

    if outcome_win is not None and status in (FILLED, PARTIALLY_FILLED):
        gross = filled_shares if outcome_win else 0.0
        fees = 0.0 if fee_rate is None else float(fee_rate) * spent
        net = gross - spent - fees
        base.update({
            "gross_payout": round(gross, 8),
            "fees": round(fees, 8),
            "net_pnl": round(net, 8),
            "max_loss": round(spent, 8),
            "return_on_risk": round(net / spent, 8) if spent > 0 else None,
        })
    return base
