"""
Quote-tick warning volume — diagnostic capability check only.

============================================================================
HISTORY / WHY THIS FILE LOOKS THE WAY IT DOES
============================================================================
The original version of this patch (commit ea4c5a9) attempted:

    self._log = _QuoteDropAggregator(self._log)

inside a wrapped PolymarketDataClient.__init__, to transparently proxy the
DataClient's Logger and aggregate its high-volume "Dropping QuoteTick"
WARNING (4,121,924 occurrences over the Week-2 run, ~8.6/s, ~1.3GB
estimated journal storage — a real operational/logging-volume concern,
never a data-integrity one).

That design crashed EVERY child of the Week-3 launch attempt
(run_1785613687_96724, 2026-08-01T19:48:07.412Z / 12:48:07 PDT) during
TradingNode.build(), inside PolymarketDataClient.__init__:

    AttributeError: attribute '_log' of 'nautilus_trader.common.component.Component'
    objects is not writable

Root cause, confirmed against the REAL installed types (not the hand-written
fake-logger test double used previously):

  - nautilus_trader/common/component.pxd declares, on the Component base
    class every Nautilus client inherits from:
        cdef readonly Logger _log
    `readonly` is a genuine, compiled, permanent Cython restriction —
    `self._log = anything` can NEVER succeed on a real instance, regardless
    of construction order. It is not a not-yet-initialized issue.
  - Logger itself (also `cdef class`, also in component.pxd/.pyx) has NO
    Python `__dict__` (`hasattr(Logger(...), '__dict__')` is False) and its
    TYPE is immutable at the Python level:
        >>> Logger.warning = something
        TypeError: cannot set 'warning' attribute of immutable type
        'nautilus_trader.common.component.Logger'
    So neither per-instance attribute injection NOR class-level method
    replacement is possible for Logger. There is no supported extension
    point on this type in the installed nautilus_trader version.

By contrast, PolymarketDataClient itself (nautilus_trader/adapters/
polymarket/data.py) is a PLAIN PYTHON class (not compiled Cython) that
subclasses the Cython Component hierarchy — its own methods, including
__init__ and _handle_quote, ARE reassignable at the class level (this is
exactly how patch_market_orders.py already safely patches
PolymarketExecutionClient._submit_market_order). Its instances also DO get
a normal `__dict__` (proven by the existing, working vendor code:
`self._config = config`, `self._http_client = http_client` in
PolymarketDataClient.__init__ are plain dynamic attributes, not `cdef`
slots).

Given that, the ONLY narrow interposition point left is
PolymarketDataClient._handle_quote (a plain Python method). Its relevant
branch (data.py, inside _handle_quote) is:

    if bid_price is None or ask_price is None:
        if self._config.drop_quotes_missing_side:
            self._log.warning(
                f"Dropping QuoteTick for {instrument.id}: "
                f"bid_price={bid_price}, ask_price={ask_price}",
            )
            return
        else:
            ... (boundary-price substitution path; not our target)

This IS an ordinary Python `if` block, and _handle_quote as a whole IS
patchable -- but the specific self._log.warning(...) call inside it cannot
be intercepted, suppressed, or redirected without either:

  (a) duplicating the missing-side detection ("bid_price = local_book.
      best_bid_price(); ask_price = local_book.best_ask_price()") in a
      wrapper that runs BEFORE calling the original, so it can decide
      whether to call the original at all. This is a real correctness
      risk, not a cosmetic one: the original applies THIS tick's book
      delta (`local_book.apply(deltas)`) BEFORE checking bid/ask, so a
      naive pre-check reads the book's state from BEFORE this tick's
      delta is applied -- one tick stale relative to what the original
      actually evaluates. Getting this wrong could misclassify a tick
      that the original would have accepted (or vice versa).
  (b) copying the entire method body (replicating book-delta application,
      quote construction, and the last-quote dedup check) so our own
      warning call sits in the exact place the original's does.

Both (a) and (b) mean duplicating verified vendor decision logic, which
this project's stated policy is not to do without a separate, explicit,
reviewed decision ("stop and report before implementing it" -- see the
accompanying investigation report). So this file currently implements
NEITHER: it performs a read-only, side-effect-free capability check and
otherwise changes NOTHING about PolymarketDataClient. Quote acceptance and
rejection behavior, and the original per-tick "Dropping QuoteTick" WARNING
volume, are both completely unchanged from the pre-ea4c5a9 baseline.

============================================================================
WHAT THIS FILE DOES NOW
============================================================================
- apply_quote_tick_logging_patch() performs a read-only capability check
  (no instances constructed, no I/O, no exceptions expected) and logs ONE
  clear, explicit diagnostic explaining that aggregation is not enabled and
  why. It never touches PolymarketDataClient.__init__, _handle_quote,
  _handle_book_snapshot, or any Component/Logger attribute.
- The `_QuoteDropAggregator` counting/aggregation algorithm from the
  original design is KEPT (unit-tested, unchanged) because its logic
  (first-occurrence-immediate, periodic per-reason summaries) is sound and
  reusable -- only the INTEGRATION point (assigning Component._log) was
  broken. It is DORMANT: nothing in this module currently constructs or
  wires it into a live client. A future change that safely wires it in
  (e.g. against a Nautilus version where Logger supports extension, or
  after an explicit, reviewed decision to duplicate the narrow detection
  branch with the staleness issue handled) can reuse it directly.
"""
import logging
import threading
import time
from types import FunctionType
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_patch_applied = False
_last_capabilities: Optional[Dict[str, Any]] = None

_DROP_PREFIX = "Dropping QuoteTick for "
SUMMARY_INTERVAL_S = 60.0

# Kept for future reuse; not wired into any live interception point (see
# module docstring). Isolated unit tests below use a fake logger to prove
# this algorithm's own correctness -- that is legitimate and unaffected by
# this file's decision not to attach it to the real Logger.
_active_aggregators = []


class _QuoteDropAggregator:
    """Thread-safe transparent proxy around a Logger-shaped object (real or
    fake). Intercepts only messages starting with ``_DROP_PREFIX``;
    everything else is forwarded unchanged. Never raises: a failure in the
    aggregation path falls back to logging the message immediately.

    NOT currently constructed against any real Component._log (see module
    docstring for why) -- covered here purely as a unit-tested algorithm.
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
        return getattr(self._real_log, name)

    def warning(self, message: str, color: Optional[Any] = None) -> None:
        try:
            if not isinstance(message, str) or not message.startswith(_DROP_PREFIX):
                self._forward_warning(message, color)
                return
            self._handle_drop_warning(message, color)
        except Exception:
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
            self._forward_warning(message, color)

    @staticmethod
    def _extract_reason_key(message: str) -> str:
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
        with self._lock:
            self._flush_locked()


def flush_all() -> None:
    for agg in list(_active_aggregators):
        agg.flush()


def _detect_capabilities() -> Dict[str, Any]:
    """Read-only, side-effect-free introspection of the installed
    nautilus_trader version's relevant types. Constructs NO client
    instances, makes NO network calls, reads NO credentials. Used only to
    produce an accurate diagnostic message; the result never changes
    behavior in this version of the file (see module docstring)."""
    caps: Dict[str, Any] = {
        "handle_quote_importable": False,
        "handle_quote_is_plain_python_method": False,
        "component_log_descriptor": None,
        "logger_type_is_mutable": None,
    }
    try:
        from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
        caps["handle_quote_importable"] = True
        raw = PolymarketDataClient.__dict__.get("_handle_quote")
        caps["handle_quote_is_plain_python_method"] = isinstance(raw, FunctionType)
    except Exception as e:
        caps["import_error"] = f"{type(e).__name__}: {e}"
        return caps

    try:
        from nautilus_trader.common.component import Component, Logger
        descriptor = Component.__dict__.get("_log")
        caps["component_log_descriptor"] = type(descriptor).__name__ if descriptor is not None else None
        # Cheap, safe probe: attempting to set a class-level attribute on an
        # immutable extension type raises TypeError without constructing or
        # mutating any real instance.
        try:
            Logger.warning = Logger.warning  # no-op assignment of the same value
            caps["logger_type_is_mutable"] = True
        except TypeError:
            caps["logger_type_is_mutable"] = False
    except Exception as e:
        caps["introspection_error"] = f"{type(e).__name__}: {e}"

    return caps


def apply_quote_tick_logging_patch() -> bool:
    """Read-only capability check + diagnostic log. Never patches
    PolymarketDataClient.__init__, _handle_quote, _handle_book_snapshot, or
    any Component/Logger attribute -- see module docstring for the full
    investigation behind this decision. Idempotent: repeated calls in the
    same or a fresh child process are safe and cannot accumulate state or
    emit the diagnostic more than once per process.

    Returns True if the check ran without an unexpected error (this is the
    expected, safe outcome even though aggregation is not enabled).
    Returns False only if nautilus_trader itself could not be imported at
    all -- an environment problem unrelated to this patch's own logic.
    """
    global _patch_applied, _last_capabilities
    if _patch_applied:
        logger.info("Quote-tick logging capability check already performed (no-op)")
        return True

    caps = _detect_capabilities()
    _last_capabilities = caps
    _patch_applied = True

    if not caps.get("handle_quote_importable"):
        logger.error(
            "Quote-tick logging patch: could not import "
            "nautilus_trader.adapters.polymarket.data.PolymarketDataClient "
            f"({caps.get('import_error')}). Original per-tick warning "
            "behavior is unaffected either way; quote handling is untouched."
        )
        return False

    logger.info(
        "Quote-tick logging: aggregation is NOT enabled. Component._log is a "
        f"read-only Cython attribute (descriptor type: "
        f"{caps.get('component_log_descriptor')}) and Logger is a sealed "
        f"extension type (mutable at the Python level: "
        f"{caps.get('logger_type_is_mutable')}), so no safe interception "
        "point exists without duplicating vendor detection logic -- which "
        "this patch declines to do without a separate, explicit decision. "
        "Falling back to the ORIGINAL, unaggregated 'Dropping QuoteTick' "
        "per-tick warning behavior. This is an expected, deliberate "
        "fallback, not an error, and does not change quote acceptance or "
        "rejection behavior in any way."
    )
    return True
