"""
Patch: aggregate the high-volume "Dropping QuoteTick" WARNING into periodic,
rate-limited summaries — LOGGING ONLY, never touches quote acceptance or
rejection behavior.

Root cause (read-only traced, vendored code, NOT modified):
  .venv/lib/python3.12/site-packages/nautilus_trader/adapters/polymarket/data.py
  PolymarketDataClient._handle_quote() intentionally drops a QuoteTick and
  logs
    self._log.warning(f"Dropping QuoteTick for {id}: bid_price=..., ask_price=...")
  whenever one side of the book is missing and
  config.drop_quotes_missing_side=True (our deployed config) — expected,
  defensive behavior near market open/resolution or thin top-of-book
  liquidity. `_handle_book_snapshot()` logs a related, much lower-volume
  message for the same reason on full snapshots.

Verified in the Week-2 run (run_1785129217_59916): 4,121,924 of these
warnings over the run's lifetime (~8.6/s, ~1.3GB of estimated journal
storage), with ZERO data-integrity or watchdog impact — a real
operational/logging-volume concern only, not a data-handling defect.

This patch wraps ONLY the DataClient's own Logger instance, at the
attribute level, after the real client is fully constructed. It never
touches `_handle_quote`, `_handle_book_snapshot`, book/quote acceptance-or-
rejection logic, or any other Nautilus method — the exact same quotes are
still dropped, under the exact same conditions, with the exact same
`return` behavior, as before this patch. Only how the resulting WARNING
text reaches the log is changed:
  - the FIRST time a given (instrument, missing-side reason) is seen, it is
    logged immediately and in full — a brand-new rejection signature is
    never hidden or delayed;
  - subsequent occurrences of an already-seen (instrument, reason) are
    counted and rolled up into one periodic INFO summary line per
    QUOTE_DROP_SUMMARY_INTERVAL_S seconds, grouped by reason;
  - any OTHER log message (including a future, differently-worded warning
    from this same component) is passed straight through, unmodified and
    immediately — never delayed, never aggregated.
"""
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_patch_applied = False

_DROP_PREFIX = "Dropping QuoteTick for "

# How often to flush an aggregated summary of suppressed duplicate warnings.
# Overridable via env for testing; 60s in production is a reasonable balance
# between log volume and freshness.
SUMMARY_INTERVAL_S = float(os.getenv("QUOTE_DROP_SUMMARY_INTERVAL_S", "60"))

# Live aggregator instances, kept only so tests / diagnostics can force a
# flush; never used to share state ACROSS instances or process generations.
_active_aggregators = []


class _QuoteDropAggregator:
    """Thread-safe transparent proxy around a real Nautilus ``Logger``.

    Intercepts only messages starting with ``_DROP_PREFIX``; everything
    else (info/debug/error, and any warning with different text) is
    forwarded to the real logger unchanged. Never raises: a failure in the
    aggregation path falls back to logging the message immediately, exactly
    as the unpatched client would have.
    """

    def __init__(self, real_log: Any, interval_s: float = SUMMARY_INTERVAL_S):
        self._real_log = real_log
        self._interval_s = interval_s
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {}
        self._seen_keys = set()
        self._last_flush = time.monotonic()
        _active_aggregators.append(self)

    def __getattr__(self, name):
        # Transparent proxy for everything we don't explicitly override
        # (info, debug, error, .name, ...) -> the real Logger, unchanged.
        return getattr(self._real_log, name)

    def warning(self, message: str, color: Optional[Any] = None) -> None:
        try:
            if not isinstance(message, str) or not message.startswith(_DROP_PREFIX):
                self._forward_warning(message, color)
                return
            self._handle_drop_warning(message, color)
        except Exception:
            # Aggregation must never suppress a real warning outright.
            self._forward_warning(message, color)

    def _forward_warning(self, message, color):
        if color is None:
            self._real_log.warning(message)
        else:
            self._real_log.warning(message, color)

    def _handle_drop_warning(self, message: str, color) -> None:
        key = self._extract_reason_key(message)
        with self._lock:
            first_time = key not in self._seen_keys
            if first_time:
                self._seen_keys.add(key)
            else:
                self._counts[key] = self._counts.get(key, 0) + 1
            self._maybe_flush_locked()
        if first_time:
            # Always visible promptly: a brand-new (instrument, reason)
            # combination is never delayed or hidden by aggregation.
            self._forward_warning(message, color)

    @staticmethod
    def _extract_reason_key(message: str) -> str:
        """Group by instrument id + which side(s) were missing, e.g.
        "Dropping QuoteTick for <id>: bid_price=None, ask_price=0.5"
        -> "<id>|bid_missing". Falls back to the raw message as the key if
        the text doesn't match the expected shape (still counted, never
        dropped)."""
        try:
            rest = message[len(_DROP_PREFIX):]
            instrument_id, fields = rest.split(":", 1)
            bid_missing = "bid_price=None" in fields
            ask_missing = "ask_price=None" in fields
            if bid_missing and ask_missing:
                reason = "both_missing"
            elif bid_missing:
                reason = "bid_missing"
            elif ask_missing:
                reason = "ask_missing"
            else:
                reason = "unknown"
            return f"{instrument_id.strip()}|{reason}"
        except Exception:
            return message

    def _maybe_flush_locked(self) -> None:
        now = time.monotonic()
        if now - self._last_flush < self._interval_s:
            return
        self._flush_locked()
        self._last_flush = now

    def _flush_locked(self) -> None:
        if not self._counts:
            return
        total = sum(self._counts.values())
        by_reason: Dict[str, int] = {}
        for key, n in self._counts.items():
            reason = key.rsplit("|", 1)[-1] if "|" in key else "unknown"
            by_reason[reason] = by_reason.get(reason, 0) + n
        n_instruments = len(self._counts)
        summary = ", ".join(f"{r}={n}" for r, n in sorted(by_reason.items()))
        self._real_log.info(
            f"Dropping QuoteTick: suppressed {total} additional identical "
            f"warning(s) across {n_instruments} instrument/reason "
            f"combination(s) in the last {self._interval_s:.0f}s (by reason: {summary})"
        )
        self._counts.clear()

    def flush(self) -> None:
        """Force an immediate flush of any pending suppressed count. Safe
        to call at any time (e.g. before shutdown); a no-op if there is
        nothing pending."""
        with self._lock:
            self._flush_locked()


def flush_all() -> None:
    """Flush every aggregator created by this patch in the current process
    (there is normally exactly one, for the single PolymarketDataClient
    instance). Diagnostic convenience only; never required for safety."""
    for agg in list(_active_aggregators):
        agg.flush()


def apply_quote_tick_logging_patch() -> bool:
    """Wrap PolymarketDataClient.__init__ so every constructed instance's
    logger aggregates the high-volume "Dropping QuoteTick" warning.

    Never touches _handle_quote, _handle_book_snapshot, or any book/quote
    acceptance-or-rejection logic — only the Logger attribute used for this
    one message pattern is replaced with a transparent, aggregating proxy.
    """
    global _patch_applied
    if _patch_applied:
        logger.info("Quote-tick logging patch already applied")
        return True

    try:
        from nautilus_trader.adapters.polymarket.data import PolymarketDataClient

        _original_init = PolymarketDataClient.__init__

        def _patched_init(self, *args, **kwargs):
            _original_init(self, *args, **kwargs)
            # Defense in depth: even if __init__ were ever invoked more than
            # once on the same instance, never wrap an already-wrapped
            # logger (would nest proxies and double-count/double-emit).
            if not isinstance(self._log, _QuoteDropAggregator):
                self._log = _QuoteDropAggregator(self._log)

        PolymarketDataClient.__init__ = _patched_init
        _patch_applied = True
        logger.info(
            "Quote-tick logging patch applied — 'Dropping QuoteTick' warnings "
            f"aggregated into periodic summaries every {SUMMARY_INTERVAL_S:.0f}s "
            "(quote acceptance/rejection behavior unchanged)"
        )
        return True
    except ImportError as e:
        logger.error(f"Failed to import required modules: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to apply quote-tick logging patch: {e}")
        return False
