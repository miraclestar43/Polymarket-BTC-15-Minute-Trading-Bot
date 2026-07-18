"""
Settlement pipeline — DRY-RUN / DIAGNOSTIC ONLY.

Turns a recorded shadow/baseline decision into an auditable settlement:

    decision -> stable market identity -> outcome (official | inferred | unavailable)
             -> P/L (binary prediction-market payout)

Design goals (see docs/nextjs_dashboard_plan.md context; this module is
diagnostic and NEVER places, signs, or submits orders):

  * Stable market identity independent of human-readable question text.
  * Explicit, non-mixed resolution source hierarchy:
        1) official_resolution        (Polymarket/Gamma resolved outcome)
        2) inferred_from_market_data  (BTC open-vs-close of the 15-min window)
        3) unavailable                (no fabricated P/L)
  * Offline-first: official outcomes are read from the on-disk resolution
    cache and inferred outcomes from the local BTC price history, so the
    pipeline is deterministic and testable without any network access.
  * Look-ahead prevention for inferred outcomes (explicit tolerance, reject
    ambiguous single-point windows).

The P/L convention is intentionally identical to the pre-existing
analyze_strategy_experiments.resolve_candidate() convention so historical
analysis stays comparable:

    size (== stake, in USD) is the maximum loss.
    shares  = stake / entry_price
    win  -> settlement value = shares * $1  => pnl = stake * (1 - entry) / entry
    loss -> settlement value = shares * $0  => pnl = -stake

No fees are modelled by default (fee=0.0); a fee hook is provided so callers
can pass an explicit assumption. This does NOT change strategy behavior.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Reuse the canonical source labels + BTC inference primitive so there is a
# single source of truth for outcome semantics.
from core.strategy_brain.market_resolver import (
    OUTCOME_OFFICIAL,
    OUTCOME_INFERRED,
    OUTCOME_UNAVAILABLE,
)

SCHEMA_VERSION = "settlement-1.0"

# A BTC "Up or Down" market lasts exactly 15 minutes.
MARKET_INTERVAL_SECONDS = 900

# Default tolerance for matching a BTC price sample to a window boundary.
# The local price history is sampled ~every 15 min, so allow up to half a
# window on each side but reject when both boundaries collapse to one sample.
DEFAULT_PRICE_TOLERANCE_SECONDS = 450.0

# Default hypothetical stake (== max loss) used for comparable P/L.
DEFAULT_STAKE = 5.0

_SLUG_PREFIX = "btc-updown-15m-"


# ---------------------------------------------------------------------------
# Stable market identity
# ---------------------------------------------------------------------------
def extract_condition_id(market_id: Optional[str]) -> Optional[str]:
    """
    Extract the 0x-prefixed 66-char condition id from a NautilusTrader
    market id of the form ``0x{condition}-{token}.POLYMARKET``.
    """
    if not market_id:
        return None
    head = market_id.split(".POLYMARKET")[0]
    first = head.split("-", 1)[0]
    if first.startswith("0x") and len(first) == 66:
        return first
    if market_id.startswith("0x") and len(market_id) == 66:
        return market_id
    return None


def extract_token_id(market_id: Optional[str]) -> Optional[str]:
    """Extract the numeric token id (the part after the condition id)."""
    if not market_id or ".POLYMARKET" not in market_id:
        return None
    head = market_id.split(".POLYMARKET")[0]
    parts = head.split("-", 1)
    if len(parts) == 2 and parts[1]:
        return parts[1]
    return None


def parse_slug_window(slug: Optional[str]) -> Tuple[Optional[float], Optional[float]]:
    """
    Derive (start_ts, end_ts) from a ``btc-updown-15m-{start_epoch}`` slug.

    The embedded epoch is the market START; the window ends 900s later.
    Returns (None, None) when the slug is missing or malformed.
    """
    if not slug or not slug.startswith(_SLUG_PREFIX):
        return None, None
    tail = slug[len(_SLUG_PREFIX):]
    try:
        start = float(int(tail))
    except (TypeError, ValueError):
        return None, None
    return start, start + MARKET_INTERVAL_SECONDS


def _parse_iso(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def market_identity(rec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a stable identity for a decision record. Prefers structured ids
    (condition id, token id) and the slug epoch over the free-text question.

    The market window is derived from ``market_end_time`` when present,
    otherwise from the slug epoch (start + 900). This is what makes the
    historical records (whose market_end_time is null) resolvable.
    """
    market_id = rec.get("market_id")
    slug = rec.get("market_slug")
    condition_id = extract_condition_id(market_id)
    token_id = extract_token_id(market_id)

    end_ts = _parse_iso(rec.get("market_end_time"))
    start_ts: Optional[float]
    if end_ts is not None:
        start_ts = end_ts - MARKET_INTERVAL_SECONDS
    else:
        start_ts, end_ts = parse_slug_window(slug)

    # A stable dedupe key: prefer condition id, then slug, then raw market id.
    stable_key = condition_id or slug or market_id or "unknown"

    return {
        "stable_key": stable_key,
        "market_id": market_id,
        "condition_id": condition_id,
        "token_id": token_id,
        "slug": slug,
        "market_start_ts": start_ts,
        "market_end_ts": end_ts,
        "question": rec.get("market_question"),
    }


# ---------------------------------------------------------------------------
# BTC price history + look-ahead-safe inference
# ---------------------------------------------------------------------------
def _nearest_point(
    points: List[Dict[str, float]], target_ts: Optional[float], tol_s: float
) -> Optional[Dict[str, float]]:
    """Nearest {ts, price} point to target within tol_s (either side), else None."""
    if not points or target_ts is None:
        return None
    best = None
    best_gap = None
    for p in points:
        gap = abs(p["ts"] - target_ts)
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = p
    if best is None or best_gap is None or best_gap > tol_s:
        return None
    return best


def infer_from_prices(
    start_ts: Optional[float],
    end_ts: Optional[float],
    price_points: List[Dict[str, float]],
    tol_s: float = DEFAULT_PRICE_TOLERANCE_SECONDS,
) -> Dict[str, Any]:
    """
    Infer a BTC 15-min UP/DOWN outcome from open-vs-close, look-ahead-safe.

    Rejects (outcome=None, source=unavailable) when:
      - either boundary is missing / outside tolerance,
      - both boundaries collapse to the SAME sample (ambiguous window),
      - the move is exactly flat.
    """
    if start_ts is None or end_ts is None:
        return {"outcome": None, "source": OUTCOME_UNAVAILABLE, "reason": "no_window"}

    open_pt = _nearest_point(price_points, start_ts, tol_s)
    close_pt = _nearest_point(price_points, end_ts, tol_s)
    if open_pt is None or close_pt is None:
        return {
            "outcome": None,
            "source": OUTCOME_UNAVAILABLE,
            "reason": "missing_boundary_price",
            "open_price": open_pt["price"] if open_pt else None,
            "close_price": close_pt["price"] if close_pt else None,
        }

    if open_pt["ts"] == close_pt["ts"]:
        return {
            "outcome": None,
            "source": OUTCOME_UNAVAILABLE,
            "reason": "single_point_window_ambiguous",
            "open_price": open_pt["price"],
            "close_price": close_pt["price"],
        }

    open_price = open_pt["price"]
    close_price = close_pt["price"]
    if close_price > open_price:
        outcome = "UP"
    elif close_price < open_price:
        outcome = "DOWN"
    else:
        return {
            "outcome": None,
            "source": OUTCOME_UNAVAILABLE,
            "reason": "flat_move_no_outcome",
            "open_price": open_price,
            "close_price": close_price,
        }

    return {
        "outcome": outcome,
        "source": OUTCOME_INFERRED,
        "reason": "btc_open_vs_close",
        "open_price": open_price,
        "close_price": close_price,
        "open_gap_s": round(abs(open_pt["ts"] - start_ts), 1),
        "close_gap_s": round(abs(close_pt["ts"] - end_ts), 1),
    }


# ---------------------------------------------------------------------------
# Official resolution (offline, from the on-disk resolution cache)
# ---------------------------------------------------------------------------
def official_from_cache(
    identity: Dict[str, Any], resolution_cache: Dict[str, Any]
) -> Optional[str]:
    """
    Return an official resolved outcome for the market from the on-disk cache,
    or None. The cache is keyed by condition id / market id / slug (whatever
    the resolver used). We only accept status == "resolved" with an outcome.
    """
    if not resolution_cache:
        return None
    for key in (
        identity.get("condition_id"),
        identity.get("market_id"),
        identity.get("slug"),
        identity.get("stable_key"),
    ):
        if not key:
            continue
        entry = resolution_cache.get(key)
        if isinstance(entry, dict) and entry.get("resolution_status") == "resolved":
            outcome = entry.get("resolved_outcome")
            if outcome:
                return str(outcome).upper()
    return None


# ---------------------------------------------------------------------------
# P/L (binary prediction-market payout) — matches historical convention
# ---------------------------------------------------------------------------
def outcome_wins_side(outcome: Optional[str], side: str) -> Optional[bool]:
    """YES/UP wins a long; NO/DOWN wins a short. None when undecidable."""
    if not outcome:
        return None
    up = outcome in ("YES", "UP")
    down = outcome in ("NO", "DOWN")
    if side == "long":
        return up if (up or down) else None
    if side == "short":
        return down if (up or down) else None
    return None


def compute_pnl(
    side: str,
    entry_price: Optional[float],
    outcome: Optional[str],
    stake: float = DEFAULT_STAKE,
    fee: float = 0.0,
) -> Tuple[Optional[bool], Optional[float]]:
    """
    Binary payout P/L. Returns (win, net_pnl).

    shares = stake / entry
    win  -> gross = stake*(1-entry)/entry ; loss -> gross = -stake
    net  = gross - fee
    Returns (None, None) when the side/entry/outcome cannot settle.
    """
    win = outcome_wins_side(outcome, side)
    if win is None:
        return None, None
    if entry_price is None or not (0.0 < float(entry_price) < 1.0):
        return win, None
    entry = float(entry_price)
    gross = (stake * (1.0 - entry) / entry) if win else -stake
    return win, round(gross - fee, 6)


# ---------------------------------------------------------------------------
# Settlement of one market identity
# ---------------------------------------------------------------------------
def settle_market(
    identity: Dict[str, Any],
    *,
    resolution_cache: Optional[Dict[str, Any]] = None,
    price_points: Optional[List[Dict[str, float]]] = None,
    now_ts: Optional[float] = None,
    official_only: bool = False,
    tol_s: float = DEFAULT_PRICE_TOLERANCE_SECONDS,
) -> Dict[str, Any]:
    """
    Resolve a single market's outcome using the source hierarchy. Produces a
    fully-labelled, auditable settlement record (no P/L here — P/L is
    per-candidate because it depends on side/entry).
    """
    resolution_cache = resolution_cache or {}
    price_points = price_points or []
    if now_ts is None:
        now_ts = datetime.now(timezone.utc).timestamp()

    end_ts = identity.get("market_end_ts")
    base = {
        "schema_version": SCHEMA_VERSION,
        "stable_key": identity.get("stable_key"),
        "market_id": identity.get("market_id"),
        "condition_id": identity.get("condition_id"),
        "token_id": identity.get("token_id"),
        "slug": identity.get("slug"),
        "market_start_ts": identity.get("market_start_ts"),
        "market_end_ts": end_ts,
        "market_end_iso": (
            datetime.fromtimestamp(end_ts, timezone.utc).isoformat()
            if end_ts is not None else None
        ),
        "resolved_outcome": None,
        "resolution_source": OUTCOME_UNAVAILABLE,
        "resolution_status": "unavailable",
        "resolution_timestamp": datetime.now(timezone.utc).isoformat(),
        "confidence": "none",
        "inference": None,
        "reason": None,
    }

    # A market that has not finished yet must never be settled (no look-ahead).
    if end_ts is None:
        base["reason"] = "no_market_window"
        return base
    if end_ts > now_ts:
        base["resolution_status"] = "open"
        base["reason"] = "market_not_finished"
        return base

    # 1) Official resolution (authoritative).
    official = official_from_cache(identity, resolution_cache)
    if official:
        base["resolved_outcome"] = official
        base["resolution_source"] = OUTCOME_OFFICIAL
        base["resolution_status"] = "resolved"
        base["confidence"] = "official"
        return base

    if official_only:
        base["reason"] = "no_official_resolution"
        return base

    # 2) Inferred from BTC price open-vs-close.
    inf = infer_from_prices(
        identity.get("market_start_ts"), end_ts, price_points, tol_s=tol_s
    )
    if inf.get("outcome"):
        base["resolved_outcome"] = inf["outcome"]
        base["resolution_source"] = OUTCOME_INFERRED
        base["resolution_status"] = "resolved"
        base["confidence"] = "inferred_low"
        base["inference"] = {
            "rule": inf.get("reason"),
            "open_price": inf.get("open_price"),
            "close_price": inf.get("close_price"),
            "open_gap_s": inf.get("open_gap_s"),
            "close_gap_s": inf.get("close_gap_s"),
            "tolerance_s": tol_s,
        }
        return base

    # 3) Unavailable.
    base["reason"] = inf.get("reason", "unavailable")
    if inf.get("open_price") is not None or inf.get("close_price") is not None:
        base["inference"] = {
            "rule": "attempted_btc_open_vs_close",
            "open_price": inf.get("open_price"),
            "close_price": inf.get("close_price"),
            "tolerance_s": tol_s,
        }
    return base


# ---------------------------------------------------------------------------
# Loaders (offline data sources)
# ---------------------------------------------------------------------------
def load_resolution_cache(log_dir: str = "logs") -> Dict[str, Any]:
    """Load the on-disk market resolution cache (offline official source)."""
    path = Path(log_dir) / "market_resolutions_cache.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_btc_price_points(log_dir: str = "logs") -> List[Dict[str, float]]:
    """Load BTC price history {ts, price} sorted by ts (offline inference source)."""
    path = Path(log_dir) / "btc_price_history.jsonl"
    points: List[Dict[str, float]] = []
    if not path.exists():
        return points
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = r.get("ts")
                price = r.get("price")
                if ts is not None and price is not None:
                    points.append({"ts": float(ts), "price": float(price)})
    except Exception:
        return []
    points.sort(key=lambda p: p["ts"])
    return points
