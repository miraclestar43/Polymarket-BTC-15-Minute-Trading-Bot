"""
Dual-token order-book store (DRY-RUN / DIAGNOSTIC ONLY).

Maintains SEPARATE order books for BOTH outcome tokens of a Polymarket binary
market (YES/Up and NO/Down), keyed by *token id* — never merely by market — so
a SHORT/BUY-NO decision can be priced from the contemporaneous NO ask instead
of reusing the YES midpoint.

Design invariants
-----------------
* Books are keyed by token id. A snapshot/update for an unknown token id is
  rejected (prevents cross-market contamination).
* A newly discovered market replaces the expired one safely: ``set_market``
  bumps the generation and clears both books, so no stale cross-market quote
  survives.
* Reconnect handling: ``on_disconnect`` invalidates both books and bumps the
  generation; books only become valid again once a fresh snapshot arrives.
* Staleness: a book older than the freshness threshold is not treated as
  executable. Crossed or out-of-range books are likewise non-executable.
* The store NEVER derives a NO quote from the YES book. ``1 - yes_mid`` is only
  ever exposed elsewhere as a clearly labelled diagnostic cross-check.

This module is pure/deterministic and makes no network calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

Ladder = List[Tuple[float, float]]  # [(price, size), ...]

# Book status taxonomy (why a book is / isn't executable).
STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_STALE = "stale"
STATUS_CROSSED = "crossed"
STATUS_OUT_OF_RANGE = "out_of_range"
STATUS_EXPIRED = "expired"

# Conservative defaults; the live bot overrides these from strategy.yaml
# (market_data: section). Kept here so unit tests need no config.
DEFAULT_FRESHNESS_SECONDS = 60.0
DEFAULT_SKEW_WARN_SECONDS = 5.0
DEFAULT_MAX_SPREAD_WARN = 0.10
DEFAULT_SUM_BID_MAX = 1.05
DEFAULT_SUM_ASK_MIN = 0.95


def _sorted_bids(levels: Ladder) -> Ladder:
    return sorted(((float(p), float(s)) for p, s in levels if s and s > 0),
                  key=lambda x: -x[0])


def _sorted_asks(levels: Ladder) -> Ladder:
    return sorted(((float(p), float(s)) for p, s in levels if s and s > 0),
                  key=lambda x: x[0])


@dataclass
class TokenBook:
    """One token's order book with provenance and validity."""
    token_id: Optional[str] = None
    bids: Ladder = field(default_factory=list)   # sorted best-first (desc price)
    asks: Ladder = field(default_factory=list)   # sorted best-first (asc price)
    quote_ts: Optional[float] = None             # epoch seconds of last update
    source: Optional[str] = None
    generation: int = -1
    valid: bool = False

    # -- top of book --------------------------------------------------------
    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_bid_size(self) -> Optional[float]:
        return self.bids[0][1] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def best_ask_size(self) -> Optional[float]:
        return self.asks[0][1] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    def age(self, now: float) -> Optional[float]:
        return None if self.quote_ts is None else now - self.quote_ts

    def is_crossed(self) -> bool:
        return (self.best_bid is not None and self.best_ask is not None
                and self.best_bid >= self.best_ask)

    def in_range(self) -> bool:
        for v in (self.best_bid, self.best_ask):
            if v is not None and not (0.0 < v < 1.0):
                return False
        return True

    def status(self, now: float, freshness_s: float, current_gen: int) -> str:
        if not self.valid or self.token_id is None or self.quote_ts is None:
            return STATUS_MISSING
        if self.generation != current_gen:
            return STATUS_EXPIRED
        if self.is_crossed():
            return STATUS_CROSSED
        if not self.in_range():
            return STATUS_OUT_OF_RANGE
        age = self.age(now)
        if age is not None and age > freshness_s:
            return STATUS_STALE
        return STATUS_OK


class DualBookStore:
    """Holds the YES and NO books for the *current* market, keyed by token id."""

    def __init__(
        self,
        freshness_seconds: float = DEFAULT_FRESHNESS_SECONDS,
        skew_warn_seconds: float = DEFAULT_SKEW_WARN_SECONDS,
        max_spread_warn: float = DEFAULT_MAX_SPREAD_WARN,
        sum_bid_max: float = DEFAULT_SUM_BID_MAX,
        sum_ask_min: float = DEFAULT_SUM_ASK_MIN,
    ) -> None:
        self.freshness_seconds = float(freshness_seconds)
        self.skew_warn_seconds = float(skew_warn_seconds)
        self.max_spread_warn = float(max_spread_warn)
        self.sum_bid_max = float(sum_bid_max)
        self.sum_ask_min = float(sum_ask_min)

        self.generation = 0
        self.condition_id: Optional[str] = None
        self.market_id: Optional[str] = None
        self.yes_token_id: Optional[str] = None
        self.no_token_id: Optional[str] = None
        self.yes = TokenBook()
        self.no = TokenBook()

    # -- lifecycle ----------------------------------------------------------
    def set_market(
        self,
        condition_id: Optional[str],
        yes_token_id: Optional[str],
        no_token_id: Optional[str],
        market_id: Optional[str] = None,
    ) -> bool:
        """
        Point the store at a (possibly new) market. When the token ids change,
        bump the generation and clear both books so no stale cross-market quote
        survives. Returns True if the market actually changed.
        """
        changed = (yes_token_id != self.yes_token_id
                   or no_token_id != self.no_token_id)
        if not changed:
            self.condition_id = condition_id or self.condition_id
            self.market_id = market_id or self.market_id
            return False
        self.generation += 1
        self.condition_id = condition_id
        self.market_id = market_id
        self.yes_token_id = yes_token_id
        self.no_token_id = no_token_id
        self.yes = TokenBook(token_id=yes_token_id, generation=self.generation)
        self.no = TokenBook(token_id=no_token_id, generation=self.generation)
        return True

    def on_disconnect(self) -> None:
        """Invalidate both books after a feed disconnect (data is now suspect)."""
        self.generation += 1
        self.yes.valid = False
        self.no.valid = False

    def on_resubscribe(self) -> None:
        """
        Mark that we have resubscribed. Books stay invalid until a fresh
        snapshot arrives (stamped with the current generation), guaranteeing
        pre-reconnect data is never treated as live.
        """
        # Nothing to do beyond leaving books invalid; kept explicit for clarity
        # and so callers can log the resubscription event.
        return None

    # -- book routing (by token id) -----------------------------------------
    def _book_for_token(self, token_id: Optional[str]) -> Optional[TokenBook]:
        if token_id is None:
            return None
        if token_id == self.yes_token_id:
            return self.yes
        if token_id == self.no_token_id:
            return self.no
        return None

    def apply_snapshot(
        self, token_id: str, bids: Ladder, asks: Ladder, ts: float,
        source: str = "clob_snapshot",
    ) -> bool:
        """Apply a full-depth snapshot routed by token id. Unknown token → reject."""
        book = self._book_for_token(token_id)
        if book is None:
            return False
        book.token_id = token_id
        book.bids = _sorted_bids(bids)
        book.asks = _sorted_asks(asks)
        book.quote_ts = float(ts)
        book.source = source
        book.generation = self.generation
        book.valid = True
        return True

    def apply_top_of_book(
        self, token_id: str, bid: Optional[float], bid_size: Optional[float],
        ask: Optional[float], ask_size: Optional[float], ts: float,
        source: str = "quote_tick",
    ) -> bool:
        """
        Apply a top-of-book update (e.g. a Nautilus QuoteTick) routed by token
        id. Produces a single-level ladder — we never fabricate deeper
        liquidity than was observed.
        """
        book = self._book_for_token(token_id)
        if book is None:
            return False
        book.token_id = token_id
        book.bids = [(float(bid), float(bid_size))] if bid is not None and bid_size else (
            [(float(bid), 0.0)] if bid is not None else [])
        book.asks = [(float(ask), float(ask_size))] if ask is not None and ask_size else (
            [(float(ask), 0.0)] if ask is not None else [])
        book.quote_ts = float(ts)
        book.source = source
        book.generation = self.generation
        book.valid = True
        return True

    # -- queries ------------------------------------------------------------
    def book_for_side(self, direction: str) -> Optional[TokenBook]:
        if direction == "long":
            return self.yes
        if direction == "short":
            return self.no
        return None

    def selected_ask_ladder(
        self, direction: str, now: float
    ) -> Optional[Ladder]:
        """Ask ladder for the taken token, only if its book is executable (OK)."""
        book = self.book_for_side(direction)
        if book is None:
            return None
        if book.status(now, self.freshness_seconds, self.generation) != STATUS_OK:
            return None
        return list(book.asks) if book.asks else None

    # -- combined snapshot --------------------------------------------------
    def snapshot_quote(self, now: float) -> Dict[str, Any]:
        """
        Build the enriched dual-book quote block. Executable fields (``*_bid`` /
        ``*_ask`` / ``*_ask_ladder``) are populated ONLY when the token's book
        is OK (fresh, uncrossed, in-range, current generation); otherwise they
        are None. Raw observed top-of-book values and per-book status are always
        recorded for audit, plus non-blocking cross-book validation flags.
        """
        y, n = self.yes, self.no
        y_status = y.status(now, self.freshness_seconds, self.generation)
        n_status = n.status(now, self.freshness_seconds, self.generation)
        y_ok = y_status == STATUS_OK
        n_ok = n_status == STATUS_OK

        y_age = y.age(now)
        n_age = n.age(now)
        skew = (abs(y.quote_ts - n.quote_ts)
                if y.quote_ts is not None and n.quote_ts is not None else None)
        newest_ts = None
        for t in (y.quote_ts, n.quote_ts):
            if t is not None:
                newest_ts = t if newest_ts is None else max(newest_ts, t)

        flags = self._validation_flags(y, n, skew)

        return {
            "condition_id": self.condition_id,
            "market_id": self.market_id,
            "yes_token_id": self.yes_token_id,
            "no_token_id": self.no_token_id,
            "book_generation": self.generation,
            "quote_source": "dual_book_ws",
            "quote_timestamp": newest_ts,
            "cross_book_skew_s": skew,
            # YES (executable only when OK)
            "yes_bid": y.best_bid if y_ok else None,
            "yes_ask": y.best_ask if y_ok else None,
            "yes_bid_size": y.best_bid_size if y_ok else None,
            "yes_ask_size": y.best_ask_size if y_ok else None,
            "yes_ask_ladder": list(y.asks) if (y_ok and y.asks) else None,
            "yes_observed_bid": y.best_bid,
            "yes_observed_ask": y.best_ask,
            "yes_quote_timestamp": y.quote_ts,
            "yes_quote_age_s": y_age,
            "yes_book_status": y_status,
            "yes_book_valid": y_ok,
            "yes_source": y.source,
            # NO (executable only when OK)
            "no_bid": n.best_bid if n_ok else None,
            "no_ask": n.best_ask if n_ok else None,
            "no_bid_size": n.best_bid_size if n_ok else None,
            "no_ask_size": n.best_ask_size if n_ok else None,
            "no_ask_ladder": list(n.asks) if (n_ok and n.asks) else None,
            "no_observed_bid": n.best_bid,
            "no_observed_ask": n.best_ask,
            "no_quote_timestamp": n.quote_ts,
            "no_quote_age_s": n_age,
            "no_book_status": n_status,
            "no_book_valid": n_ok,
            "no_source": n.source,
            "validation_flags": flags,
        }

    def _validation_flags(
        self, y: TokenBook, n: TokenBook, skew: Optional[float]
    ) -> Dict[str, bool]:
        """Non-blocking data-quality flags. True = healthy (no anomaly)."""
        def _in01(v: Optional[float]) -> bool:
            return v is None or (0.0 < v < 1.0)

        prices_in_range = all(_in01(v) for v in (
            y.best_bid, y.best_ask, n.best_bid, n.best_ask))

        yes_bid_le_ask = (y.best_bid is None or y.best_ask is None
                          or y.best_bid <= y.best_ask)
        no_bid_le_ask = (n.best_bid is None or n.best_ask is None
                         or n.best_bid <= n.best_ask)

        tokens_distinct = (self.yes_token_id is not None
                           and self.no_token_id is not None
                           and self.yes_token_id != self.no_token_id)

        skew_ok = skew is None or skew <= self.skew_warn_seconds

        yes_spread_ok = y.spread is None or y.spread <= self.max_spread_warn
        no_spread_ok = n.spread is None or n.spread <= self.max_spread_warn

        if y.best_bid is not None and n.best_bid is not None:
            sum_bid_ok = (y.best_bid + n.best_bid) <= self.sum_bid_max
        else:
            sum_bid_ok = True
        if y.best_ask is not None and n.best_ask is not None:
            sum_ask_ok = (y.best_ask + n.best_ask) >= self.sum_ask_min
        else:
            sum_ask_ok = True

        return {
            "yes_bid_le_ask": yes_bid_le_ask,
            "no_bid_le_ask": no_bid_le_ask,
            "prices_in_range": prices_in_range,
            "tokens_distinct": tokens_distinct,
            "cross_book_skew_ok": skew_ok,
            "yes_spread_ok": yes_spread_ok,
            "no_spread_ok": no_spread_ok,
            "sum_bid_not_above_1": sum_bid_ok,
            "sum_ask_not_below_1": sum_ask_ok,
        }
