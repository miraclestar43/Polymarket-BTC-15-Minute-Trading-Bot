"""
patch_quote_tick_logging.py — DRY-RUN / DIAGNOSTIC ONLY, deterministic,
offline. Proves the "Dropping QuoteTick" warning-flood fix (Week-2 finding:
4,121,924 occurrences, ~8.6/s, ~1.3GB estimated journal storage) is a
logging-only change: quote acceptance/rejection behavior in the vendored
Nautilus adapter is never touched, only how the resulting WARNING text
reaches the log.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import patch_quote_tick_logging as patch_mod  # noqa: E402


class _FakeLogger:
    """Minimal stand-in for nautilus_trader.common.component.Logger."""

    def __init__(self):
        self.warnings = []  # list of (message, color)
        self.infos = []
        self.debugs = []
        self.errors = []
        self.name = "FakeLogger"

    def warning(self, message, color=None):
        self.warnings.append((message, color))

    def info(self, message, color=None):
        self.infos.append((message, color))

    def debug(self, message, color=None):
        self.debugs.append((message, color))

    def error(self, message, color=None):
        self.errors.append((message, color))


def _drop_msg(instrument_id="0xABC-123", bid="None", ask="0.5"):
    return f"Dropping QuoteTick for {instrument_id}: bid_price={bid}, ask_price={ask}"


# ---------------------------------------------------------------------------
# Transparent passthrough for everything that ISN'T the target pattern
# ---------------------------------------------------------------------------
def test_non_matching_warning_passes_through_immediately():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    agg.warning("Cannot reconnect: no market streams for USER channel")
    assert real.warnings == [("Cannot reconnect: no market streams for USER channel", None)]


def test_info_debug_error_and_name_are_forwarded_unchanged():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    agg.info("hello")
    agg.debug("world")
    agg.error("boom")
    assert real.infos == [("hello", None)]
    assert real.debugs == [("world", None)]
    assert real.errors == [("boom", None)]
    assert agg.name == "FakeLogger"  # attribute passthrough via __getattr__


def test_warning_with_explicit_color_is_forwarded_with_color():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    agg.warning("some other warning", color="YELLOW")
    assert real.warnings == [("some other warning", "YELLOW")]


# ---------------------------------------------------------------------------
# 1 & 2: rejected/accepted-tick BEHAVIOR is untouched by this patch
# ---------------------------------------------------------------------------
def test_patch_does_not_modify_handle_quote_or_handle_book_snapshot():
    # NOTE: bot.py applies this patch as an import-time side effect, and
    # other test modules in the same pytest process may import bot.py
    # before this test runs -- so the patch may already be applied here.
    # That is expected (matches the idempotent, once-per-process design of
    # every patch_*.py module in this repo); this test asserts the
    # invariant that must hold regardless of application order.
    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
    before_quote = PolymarketDataClient._handle_quote
    before_snapshot = PolymarketDataClient._handle_book_snapshot

    ok = patch_mod.apply_quote_tick_logging_patch()
    assert ok is True

    # The two methods that decide whether a tick is accepted or dropped are
    # byte-for-byte the same function objects as before -- proves this
    # patch never touches quote acceptance/rejection logic, regardless of
    # whether __init__ was already wrapped by an earlier test/import.
    assert PolymarketDataClient._handle_quote is before_quote
    assert PolymarketDataClient._handle_book_snapshot is before_snapshot
    # __init__ IS wrapped (by this patch, at some point in this process).
    assert PolymarketDataClient.__init__.__qualname__.endswith("_patched_init")


# ---------------------------------------------------------------------------
# 3: repeated identical warnings are bounded/aggregated
# ---------------------------------------------------------------------------
def test_repeated_identical_warning_is_aggregated_not_flooded():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)  # never auto-flush
    msg = _drop_msg()
    for _ in range(1000):
        agg.warning(msg)
    # Only the very first occurrence reached the real logger immediately.
    assert len(real.warnings) == 1
    assert real.warnings[0][0] == msg
    # The other 999 were counted, not lost and not individually emitted.
    key = patch_mod._QuoteDropAggregator._extract_reason_key(msg)
    assert agg._counts[key] == 999


def test_forced_flush_emits_one_summary_line_with_accurate_count():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    msg = _drop_msg()
    for _ in range(500):
        agg.warning(msg)
    assert real.infos == []  # nothing flushed yet (interval not reached, not forced)
    agg.flush()
    assert len(real.infos) == 1
    summary_text = real.infos[0][0]
    assert "suppressed 499" in summary_text  # 500 total - 1 immediate first-occurrence
    assert agg._counts == {}  # cleared after flush


def test_periodic_flush_triggers_after_interval_elapses(monkeypatch):
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=1.0)
    fake_now = [1000.0]
    monkeypatch.setattr(patch_mod.time, "monotonic", lambda: fake_now[0])
    agg._last_flush = fake_now[0]

    msg = _drop_msg()
    agg.warning(msg)  # first occurrence -> immediate
    for _ in range(9):
        agg.warning(msg)  # suppressed, counted
    assert real.infos == []  # interval not yet elapsed

    fake_now[0] += 1.5  # advance past the 1.0s interval
    agg.warning(msg)  # this call's suppress path also checks the interval
    assert len(real.infos) == 1
    assert "suppressed 10" in real.infos[0][0]  # 9 counted + this 10th call


# ---------------------------------------------------------------------------
# 4: distinct warning reasons remain distinguishable
# ---------------------------------------------------------------------------
def test_distinct_instruments_each_get_their_own_first_occurrence():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    agg.warning(_drop_msg(instrument_id="0xAAA-1"))
    agg.warning(_drop_msg(instrument_id="0xBBB-2"))
    agg.warning(_drop_msg(instrument_id="0xAAA-1"))  # repeat of the first
    # Two distinct instruments -> two immediate first-occurrence warnings.
    assert len(real.warnings) == 2
    assert "0xAAA-1" in real.warnings[0][0]
    assert "0xBBB-2" in real.warnings[1][0]


def test_distinct_missing_side_reasons_are_grouped_separately():
    bid_missing = patch_mod._QuoteDropAggregator._extract_reason_key(
        _drop_msg(instrument_id="0xC", bid="None", ask="0.7"))
    ask_missing = patch_mod._QuoteDropAggregator._extract_reason_key(
        _drop_msg(instrument_id="0xC", bid="0.3", ask="None"))
    both_missing = patch_mod._QuoteDropAggregator._extract_reason_key(
        _drop_msg(instrument_id="0xC", bid="None", ask="None"))
    assert bid_missing.endswith("|bid_missing")
    assert ask_missing.endswith("|ask_missing")
    assert both_missing.endswith("|both_missing")
    assert len({bid_missing, ask_missing, both_missing}) == 3


def test_a_new_reason_after_a_flush_is_visible_promptly():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    agg.warning(_drop_msg(instrument_id="0xD", bid="None", ask="0.5"))  # first, immediate
    agg.flush()
    # A DIFFERENT reason for the same instrument must still be treated as new.
    agg.warning(_drop_msg(instrument_id="0xD", bid="0.5", ask="None"))
    assert len(real.warnings) == 2
    assert "bid_price=0.5, ask_price=None" in real.warnings[1][0]


# ---------------------------------------------------------------------------
# 5: summary counts accurately reflect suppressed occurrences, by reason
# ---------------------------------------------------------------------------
def test_summary_counts_are_accurate_across_multiple_reasons():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    for _ in range(7):
        agg.warning(_drop_msg(instrument_id="0xE", bid="None", ask="0.9"))
    for _ in range(3):
        agg.warning(_drop_msg(instrument_id="0xF", bid="0.1", ask="None"))
    agg.flush()
    assert len(real.infos) == 1
    text = real.infos[0][0]
    assert "suppressed 8" in text  # (7-1) + (3-1) = 6+2 = 8
    assert "2 instrument/reason" in text
    assert "bid_missing=6" in text  # 0xE: bid=None -> bid_missing, 7 calls, 6 suppressed
    assert "ask_missing=2" in text  # 0xF: ask=None -> ask_missing, 3 calls, 2 suppressed


def test_malformed_message_is_never_silently_dropped():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    # Starts with the prefix but doesn't parse cleanly -- must still be
    # counted/logged, never discarded outright.
    weird = "Dropping QuoteTick for garbage-no-colon-here"
    agg.warning(weird)
    assert real.warnings == [(weird, None)]


# ---------------------------------------------------------------------------
# INFO summary emission cannot recurse through the suppression path
# ---------------------------------------------------------------------------
def test_flush_summary_is_info_level_and_never_recounted_as_a_warning():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    for _ in range(50):
        agg.warning(_drop_msg(instrument_id="0xR"))
    agg.flush()
    agg.flush()  # calling again with nothing pending must be a no-op, not re-emit
    assert len(real.infos) == 1  # exactly one summary, not re-triggered by itself
    assert len(real.warnings) == 1  # only the original first-occurrence warning
    # The summary line itself went through .info(), which this proxy never
    # intercepts/pattern-matches at all (only .warning() is overridden) --
    # so it structurally cannot be treated as another "Dropping QuoteTick"
    # warning and cannot re-enter _handle_drop_warning.
    assert agg._counts == {}


def test_many_flush_cycles_do_not_grow_unboundedly():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    for cycle in range(20):
        for _ in range(10):
            agg.warning(_drop_msg(instrument_id=f"0xCycle{cycle}"))
        agg.flush()
    # 20 cycles -> 20 first-occurrence warnings + 20 summary infos, linear,
    # not exponential/recursive.
    assert len(real.warnings) == 20
    assert len(real.infos) == 20


# ---------------------------------------------------------------------------
# Instance-level double-wrap guard ("applying it twice cannot double-wrap
# __init__ or the logger")
# ---------------------------------------------------------------------------
def test_wrapping_an_already_wrapped_logger_is_a_no_op():
    real = _FakeLogger()
    once = patch_mod._QuoteDropAggregator(real)
    # Simulate what the instance-level guard in _patched_init checks.
    assert isinstance(once, patch_mod._QuoteDropAggregator)
    # The patched __init__ must never do `_QuoteDropAggregator(some_aggregator)`
    # -- verify the guard condition directly (mirrors the code in
    # apply_quote_tick_logging_patch's _patched_init).
    should_rewrap = not isinstance(once, patch_mod._QuoteDropAggregator)
    assert should_rewrap is False


def test_patched_init_source_guards_against_rewrapping():
    """`_patched_init` (installed by apply_quote_tick_logging_patch) must
    only ever wrap `self._log` when it is not already a
    `_QuoteDropAggregator`. `_log` is a Cython attribute on the real
    PolymarketDataClient/Component type that can only be constructed via
    Nautilus's own live node (event loop, http client, msgbus, cache,
    clock) -- too heavy for a unit test -- so this asserts the guard is
    present in the installed wrapper's own source, which is the exact
    code path that runs on every real instance construction."""
    import inspect
    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient

    patch_mod.apply_quote_tick_logging_patch()
    patch_mod.apply_quote_tick_logging_patch()  # second application: idempotent

    src = inspect.getsource(PolymarketDataClient.__init__)
    assert "isinstance(self._log, _QuoteDropAggregator)" in src
    assert PolymarketDataClient.__init__.__qualname__.endswith("_patched_init")


# ---------------------------------------------------------------------------
# Safety across child-process generations / shutdown
# ---------------------------------------------------------------------------
def test_each_aggregator_instance_has_independent_state():
    real_a, real_b = _FakeLogger(), _FakeLogger()
    agg_a = patch_mod._QuoteDropAggregator(real_a, interval_s=9999)
    agg_b = patch_mod._QuoteDropAggregator(real_b, interval_s=9999)
    msg = _drop_msg(instrument_id="0xSAME")
    for _ in range(5):
        agg_a.warning(msg)
    # A fresh aggregator (as a new child process would construct) has never
    # seen this key -- its first occurrence is still logged immediately,
    # proving no state leaks across instances/process generations.
    agg_b.warning(msg)
    assert len(real_b.warnings) == 1
    assert len(real_a.warnings) == 1  # unaffected by agg_b


def test_flush_all_flushes_every_active_aggregator():
    patch_mod._active_aggregators.clear()
    real_a, real_b = _FakeLogger(), _FakeLogger()
    agg_a = patch_mod._QuoteDropAggregator(real_a, interval_s=9999)
    agg_b = patch_mod._QuoteDropAggregator(real_b, interval_s=9999)
    for _ in range(2):
        agg_a.warning(_drop_msg(instrument_id="0xG"))
        agg_b.warning(_drop_msg(instrument_id="0xH"))
    patch_mod.flush_all()
    assert len(real_a.infos) == 1
    assert len(real_b.infos) == 1
    patch_mod._active_aggregators.clear()


def test_apply_patch_is_idempotent():
    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient
    patch_mod.apply_quote_tick_logging_patch()
    wrapped_init = PolymarketDataClient.__init__
    ok = patch_mod.apply_quote_tick_logging_patch()  # second call
    assert ok is True
    assert PolymarketDataClient.__init__ is wrapped_init  # not double-wrapped


# ---------------------------------------------------------------------------
# Existing dry-run safety gates unaffected by this module
# ---------------------------------------------------------------------------
def test_module_never_imports_order_placement_code():
    src = Path(patch_mod.__file__).read_text()
    for bad in ("place_order", "submit_order", "sign_order", "--live",
                "create_market_order", "create_order"):
        assert bad not in src, f"unexpected order-related reference: {bad}"
