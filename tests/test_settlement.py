"""
Settlement pipeline tests — DRY-RUN / DIAGNOSTIC ONLY.

Deterministic, offline (no network, no live APIs). Covers:
  * official YES / NO settlement
  * inferred UP / DOWN outcome
  * ambiguous / missing BTC data
  * unresolved / open (unfinished) market
  * LONG / SHORT P/L
  * entry prices near 0 and 1
  * source separation (official vs inferred never silently mixed)
  * historical schema compatibility (market_end_time null -> slug window)
  * look-ahead prevention (single-point window rejected)
  * no strategy-behavior regression (module places no orders / no bot import)
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.strategy_brain import settlement as st
from core.strategy_brain.market_resolver import (
    OUTCOME_OFFICIAL,
    OUTCOME_INFERRED,
    OUTCOME_UNAVAILABLE,
)

_COND = "0x" + "a" * 64  # 66-char condition id
_MID = f"{_COND}-12345.POLYMARKET"
_SLUG = "btc-updown-15m-1782437400"  # start=1782437400, end=+900
_END = 1782437400 + 900
_NOW = _END + 3600  # market finished an hour ago


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------
def test_extract_condition_and_token_id():
    assert st.extract_condition_id(_MID) == _COND
    assert st.extract_token_id(_MID) == "12345"
    assert st.extract_condition_id(None) is None
    assert st.extract_token_id("badformat") is None


def test_slug_window_parsing():
    start, end = st.parse_slug_window(_SLUG)
    assert start == 1782437400
    assert end == 1782437400 + 900
    assert st.parse_slug_window("not-a-slug") == (None, None)
    assert st.parse_slug_window(None) == (None, None)


def test_historical_schema_compat_uses_slug_when_end_null():
    # Historical records have market_end_time == null; window must come from slug.
    rec = {"market_id": _MID, "market_slug": _SLUG, "market_end_time": None}
    ident = st.market_identity(rec)
    assert ident["condition_id"] == _COND
    assert ident["market_end_ts"] == _END
    assert ident["market_start_ts"] == _END - 900
    assert ident["stable_key"] == _COND


# --------------------------------------------------------------------------
# Official settlement
# --------------------------------------------------------------------------
def _official_cache(outcome):
    return {_COND: {"resolution_status": "resolved", "resolved_outcome": outcome}}


def test_official_yes_settlement():
    ident = st.market_identity({"market_id": _MID, "market_slug": _SLUG})
    s = st.settle_market(ident, resolution_cache=_official_cache("YES"),
                         price_points=[], now_ts=_NOW)
    assert s["resolution_status"] == "resolved"
    assert s["resolution_source"] == OUTCOME_OFFICIAL
    assert s["resolved_outcome"] == "YES"
    assert s["confidence"] == "official"


def test_official_no_settlement():
    ident = st.market_identity({"market_id": _MID, "market_slug": _SLUG})
    s = st.settle_market(ident, resolution_cache=_official_cache("NO"),
                         price_points=[], now_ts=_NOW)
    assert s["resolved_outcome"] == "NO"
    assert s["resolution_source"] == OUTCOME_OFFICIAL


def test_official_preferred_over_inference():
    # BTC rose (would infer UP) but official says NO -> official wins, no mixing.
    pts = [{"ts": _END - 900, "price": 100.0}, {"ts": _END, "price": 110.0}]
    ident = st.market_identity({"market_id": _MID, "market_slug": _SLUG})
    s = st.settle_market(ident, resolution_cache=_official_cache("NO"),
                         price_points=pts, now_ts=_NOW)
    assert s["resolution_source"] == OUTCOME_OFFICIAL
    assert s["resolved_outcome"] == "NO"
    assert s["inference"] is None


# --------------------------------------------------------------------------
# Inferred settlement
# --------------------------------------------------------------------------
def test_inferred_up_outcome():
    pts = [{"ts": _END - 900, "price": 100.0}, {"ts": _END, "price": 110.0}]
    ident = st.market_identity({"market_id": _MID, "market_slug": _SLUG})
    s = st.settle_market(ident, resolution_cache={}, price_points=pts, now_ts=_NOW)
    assert s["resolution_status"] == "resolved"
    assert s["resolution_source"] == OUTCOME_INFERRED
    assert s["resolved_outcome"] == "UP"
    assert s["confidence"] == "inferred_low"
    assert s["inference"]["open_price"] == 100.0


def test_inferred_down_outcome():
    pts = [{"ts": _END - 900, "price": 110.0}, {"ts": _END, "price": 100.0}]
    ident = st.market_identity({"market_id": _MID, "market_slug": _SLUG})
    s = st.settle_market(ident, resolution_cache={}, price_points=pts, now_ts=_NOW)
    assert s["resolved_outcome"] == "DOWN"
    assert s["resolution_source"] == OUTCOME_INFERRED


def test_official_only_never_infers():
    pts = [{"ts": _END - 900, "price": 100.0}, {"ts": _END, "price": 110.0}]
    ident = st.market_identity({"market_id": _MID, "market_slug": _SLUG})
    s = st.settle_market(ident, resolution_cache={}, price_points=pts,
                         now_ts=_NOW, official_only=True)
    assert s["resolution_status"] == "unavailable"
    assert s["resolution_source"] == OUTCOME_UNAVAILABLE


# --------------------------------------------------------------------------
# Ambiguous / missing / open
# --------------------------------------------------------------------------
def test_missing_btc_data_unavailable():
    ident = st.market_identity({"market_id": _MID, "market_slug": _SLUG})
    s = st.settle_market(ident, resolution_cache={}, price_points=[], now_ts=_NOW)
    assert s["resolution_status"] == "unavailable"
    assert s["resolved_outcome"] is None
    assert s["reason"] == "missing_boundary_price"


def test_single_point_window_is_ambiguous():
    # A lone sample equidistant from both boundaries collapses open==close.
    pts = [{"ts": _END - 450, "price": 100.0}]  # 450s from start and from end
    ident = st.market_identity({"market_id": _MID, "market_slug": _SLUG})
    s = st.settle_market(ident, resolution_cache={}, price_points=pts, now_ts=_NOW)
    assert s["resolution_status"] == "unavailable"
    assert s["reason"] == "single_point_window_ambiguous"


def test_flat_move_unavailable():
    pts = [{"ts": _END - 900, "price": 100.0}, {"ts": _END, "price": 100.0}]
    ident = st.market_identity({"market_id": _MID, "market_slug": _SLUG})
    s = st.settle_market(ident, resolution_cache={}, price_points=pts, now_ts=_NOW)
    assert s["resolution_status"] == "unavailable"
    assert s["reason"] == "flat_move_no_outcome"


def test_open_market_not_settled():
    # Market ends in the future -> never settle (no look-ahead).
    pts = [{"ts": _END - 900, "price": 100.0}, {"ts": _END, "price": 110.0}]
    ident = st.market_identity({"market_id": _MID, "market_slug": _SLUG})
    s = st.settle_market(ident, resolution_cache=_official_cache("YES"),
                         price_points=pts, now_ts=_END - 100)
    assert s["resolution_status"] == "open"
    assert s["resolved_outcome"] is None


def test_no_window_unavailable():
    ident = st.market_identity({"market_id": _MID, "market_slug": None,
                                "market_end_time": None})
    s = st.settle_market(ident, resolution_cache={}, price_points=[], now_ts=_NOW)
    assert s["resolution_status"] == "unavailable"
    assert s["reason"] == "no_market_window"


# --------------------------------------------------------------------------
# P/L
# --------------------------------------------------------------------------
def test_long_pnl_win_and_loss():
    win, pnl = st.compute_pnl("long", 0.5, "UP", stake=5.0)
    assert win is True
    assert abs(pnl - 5.0) < 1e-9  # (1-0.5)/0.5 * 5 = 5
    win, pnl = st.compute_pnl("long", 0.5, "DOWN", stake=5.0)
    assert win is False
    assert pnl == -5.0


def test_short_pnl_win_and_loss():
    # entry_price is the YES price. A short BUYS the NO token, whose executable
    # price is 1 - YES = 0.75, so the win pays (1-0.75)/0.75 * 5 = 1.6667 (NOT
    # the buggy 15.0 that used the YES price 0.25 as the NO cost).
    win, pnl = st.compute_pnl("short", 0.25, "DOWN", stake=5.0)
    assert win is True
    assert abs(pnl - (0.75 / 0.75 * 0.0 + (1 - 0.75) / 0.75 * 5.0)) < 1e-6
    assert abs(pnl - 1.666667) < 1e-5
    win, pnl = st.compute_pnl("short", 0.25, "UP", stake=5.0)
    assert win is False
    assert pnl == -5.0


def test_short_prices_no_token_never_yes():
    # A short at YES=0.02 must be priced against the NO token (cost 0.98,
    # ~+2% on a win), NOT the YES price 0.02 (which would fake a ~49x payout).
    win, pnl = st.compute_pnl("short", 0.02, "DOWN", stake=5.0)
    assert win is True
    # NO exec = 0.98 -> gross = 5*(1-0.98)/0.98 ~= 0.102
    assert abs(pnl - (5.0 * (1 - 0.98) / 0.98)) < 1e-6
    assert pnl < 0.2  # nowhere near the buggy ~245
    # explicit override still honored when the executable price is known
    win2, pnl2 = st.compute_pnl("short", 0.02, "DOWN", stake=5.0,
                                selected_exec_price=0.98)
    assert abs(pnl - pnl2) < 1e-9


def test_entry_price_near_zero_and_one():
    # Near 1.0: tiny upside, full downside.
    win, pnl = st.compute_pnl("long", 0.99, "UP", stake=5.0)
    assert win is True
    assert abs(pnl - (5.0 * 0.01 / 0.99)) < 1e-5
    # Near 0.0: huge upside multiple.
    win, pnl = st.compute_pnl("long", 0.01, "UP", stake=5.0)
    assert win is True
    assert abs(pnl - (5.0 * 0.99 / 0.01)) < 1e-3
    # Degenerate entries (0 or 1) -> no P/L (win still known).
    win, pnl = st.compute_pnl("long", 0.0, "UP")
    assert win is True and pnl is None
    win, pnl = st.compute_pnl("long", 1.0, "UP")
    assert pnl is None


def test_fee_applied():
    win, pnl = st.compute_pnl("long", 0.5, "UP", stake=5.0, fee=0.1)
    assert abs(pnl - 4.9) < 1e-9


def test_undecidable_side_returns_none():
    win, pnl = st.compute_pnl("blocked_by_direction", 0.5, "UP")
    assert win is None and pnl is None


# --------------------------------------------------------------------------
# Source separation & no-regression
# --------------------------------------------------------------------------
def test_outcome_wins_side_mapping():
    assert st.outcome_wins_side("YES", "long") is True
    assert st.outcome_wins_side("UP", "long") is True
    assert st.outcome_wins_side("NO", "long") is False
    assert st.outcome_wins_side("DOWN", "short") is True
    assert st.outcome_wins_side("YES", "short") is False
    assert st.outcome_wins_side(None, "long") is None


def test_settlement_module_places_no_orders():
    src = Path(st.__file__).read_text()
    for bad in ("place_real_order", "_place_real_order", "--live", "\nimport bot"):
        assert bad not in src, f"settlement module must not reference {bad}"
