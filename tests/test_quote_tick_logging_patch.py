"""
patch_quote_tick_logging.py — DRY-RUN / DIAGNOSTIC ONLY, offline.

Covers two things:

1. The `_QuoteDropAggregator` counting/summarization ALGORITHM (unit-tested
   against a fake logger double, exactly as before). This class is kept for
   potential future reuse but is currently DORMANT -- nothing in this
   module constructs or wires it into a live client (see the module
   docstring in patch_quote_tick_logging.py for the full investigation).

2. The REAL compatibility fix (commit ea4c5a9 -> this commit). ea4c5a9
   crashed every real PolymarketDataClient construction with:

       AttributeError: attribute '_log' of 'nautilus_trader.common.component.
       Component' objects is not writable

   because it wrapped PolymarketDataClient.__init__ to do
   `self._log = _QuoteDropAggregator(self._log)`, and Component._log is a
   compiled `cdef readonly Logger _log` slot -- permanently unassignable
   from Python. The tests below reproduce that exact failure mode against
   the REAL, installed Cython types (not a handwritten stand-in class), and
   prove the current apply_quote_tick_logging_patch() never attempts it:
   PolymarketDataClient.__init__ is left completely untouched, and a
   genuine PolymarketDataClient constructed through Nautilus's own
   TradingNode.build() (not `__new__`) succeeds and keeps its original,
   unproxied Logger.
"""
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

import patch_quote_tick_logging as patch_mod  # noqa: E402
import data_client_construction_smoke_test as smoke_test  # noqa: E402


class _FakeLogger:
    """Minimal stand-in for nautilus_trader.common.component.Logger, used
    ONLY to unit-test the _QuoteDropAggregator algorithm in isolation. Per
    Gate 4, this is not the sole proof of compatibility with the real
    Cython types -- see the real-construction tests below."""

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
# _QuoteDropAggregator algorithm -- unit tests against a fake logger double
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


def test_repeated_identical_warning_is_aggregated_not_flooded():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)  # never auto-flush
    msg = _drop_msg()
    for _ in range(1000):
        agg.warning(msg)
    assert len(real.warnings) == 1
    assert real.warnings[0][0] == msg
    key = patch_mod._QuoteDropAggregator._extract_reason_key(msg)
    assert agg._counts[key] == 999


def test_forced_flush_emits_one_summary_line_with_accurate_count():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    msg = _drop_msg()
    for _ in range(500):
        agg.warning(msg)
    assert real.infos == []
    agg.flush()
    assert len(real.infos) == 1
    summary_text = real.infos[0][0]
    assert "suppressed 499" in summary_text
    assert agg._counts == {}


def test_periodic_flush_triggers_after_interval_elapses(monkeypatch):
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=1.0)
    fake_now = [1000.0]
    monkeypatch.setattr(patch_mod.time, "monotonic", lambda: fake_now[0])
    agg._last_flush = fake_now[0]

    msg = _drop_msg()
    agg.warning(msg)
    for _ in range(9):
        agg.warning(msg)
    assert real.infos == []

    fake_now[0] += 1.5
    agg.warning(msg)
    assert len(real.infos) == 1
    assert "suppressed 10" in real.infos[0][0]


def test_distinct_instruments_each_get_their_own_first_occurrence():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    agg.warning(_drop_msg(instrument_id="0xAAA-1"))
    agg.warning(_drop_msg(instrument_id="0xBBB-2"))
    agg.warning(_drop_msg(instrument_id="0xAAA-1"))
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
    agg.warning(_drop_msg(instrument_id="0xD", bid="None", ask="0.5"))
    agg.flush()
    agg.warning(_drop_msg(instrument_id="0xD", bid="0.5", ask="None"))
    assert len(real.warnings) == 2
    assert "bid_price=0.5, ask_price=None" in real.warnings[1][0]


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
    weird = "Dropping QuoteTick for garbage-no-colon-here"
    agg.warning(weird)
    assert real.warnings == [(weird, None)]


def test_flush_summary_is_info_level_and_never_recounted_as_a_warning():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    for _ in range(50):
        agg.warning(_drop_msg(instrument_id="0xR"))
    agg.flush()
    agg.flush()
    assert len(real.infos) == 1
    assert len(real.warnings) == 1
    assert agg._counts == {}


def test_many_flush_cycles_do_not_grow_unboundedly():
    real = _FakeLogger()
    agg = patch_mod._QuoteDropAggregator(real, interval_s=9999)
    for cycle in range(20):
        for _ in range(10):
            agg.warning(_drop_msg(instrument_id=f"0xCycle{cycle}"))
        agg.flush()
    assert len(real.warnings) == 20
    assert len(real.infos) == 20


def test_each_aggregator_instance_has_independent_state():
    real_a, real_b = _FakeLogger(), _FakeLogger()
    agg_a = patch_mod._QuoteDropAggregator(real_a, interval_s=9999)
    agg_b = patch_mod._QuoteDropAggregator(real_b, interval_s=9999)
    msg = _drop_msg(instrument_id="0xSAME")
    for _ in range(5):
        agg_a.warning(msg)
    agg_b.warning(msg)
    assert len(real_b.warnings) == 1
    assert len(real_a.warnings) == 1


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


# ---------------------------------------------------------------------------
# Existing dry-run safety gates unaffected by this module
# ---------------------------------------------------------------------------
def test_module_never_imports_order_placement_code():
    src = Path(patch_mod.__file__).read_text()
    for bad in ("place_order", "submit_order", "sign_order", "--live",
                "create_market_order", "create_order"):
        assert bad not in src, f"unexpected order-related reference: {bad}"


# ---------------------------------------------------------------------------
# Gate 4 -- the REAL compatibility fix, proven against real Nautilus types.
# ---------------------------------------------------------------------------
def test_module_source_never_assigns_component_log():
    """Static, durable proof that the ea4c5a9 defect pattern cannot exist
    in this file: no executable code assigns to any `._log` attribute.
    (The module's docstring quotes the old ea4c5a9 line for historical
    explanation -- an AST walk of actual statements ignores docstrings and
    checks only real assignment/augmented-assignment targets.)"""
    import ast

    tree = ast.parse(Path(patch_mod.__file__).read_text())
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Attribute) and t.attr == "_log":
                raise AssertionError(
                    f"found an assignment to a `._log` attribute at line {node.lineno}"
                )


def test_module_source_never_patches_init_or_handle_quote():
    """Gate 3 explicitly requires removing all attempts to patch
    PolymarketDataClient.__init__ merely to swap the logger. This module
    must not reassign __init__, _handle_quote, or _handle_book_snapshot at
    all in its current (diagnostic-only) design."""
    src = Path(patch_mod.__file__).read_text()
    for forbidden in (
        "PolymarketDataClient.__init__ =",
        "PolymarketDataClient._handle_quote =",
        "PolymarketDataClient._handle_book_snapshot =",
    ):
        assert forbidden not in src, f"unexpected reassignment: {forbidden}"


def test_polymarket_data_client_init_is_left_completely_unpatched():
    """Regardless of import order or how many times the patch is applied,
    PolymarketDataClient.__init__ must remain the genuine, original method
    -- never wrapped, never renamed to a *_patched_init variant."""
    from nautilus_trader.adapters.polymarket.data import PolymarketDataClient

    patch_mod.apply_quote_tick_logging_patch()
    patch_mod.apply_quote_tick_logging_patch()  # idempotent, still a no-op on __init__

    assert PolymarketDataClient.__init__.__qualname__ == "PolymarketDataClient.__init__"
    assert not PolymarketDataClient.__init__.__qualname__.endswith("_patched_init")


def test_apply_patch_is_idempotent_and_returns_true():
    ok1 = patch_mod.apply_quote_tick_logging_patch()
    ok2 = patch_mod.apply_quote_tick_logging_patch()
    assert ok1 is True
    assert ok2 is True


def test_apply_patch_logs_a_clear_diagnostic(caplog):
    import importlib
    importlib.reload(patch_mod)
    with caplog.at_level("INFO", logger="patch_quote_tick_logging"):
        ok = patch_mod.apply_quote_tick_logging_patch()
    assert ok is True
    messages = " ".join(r.message for r in caplog.records)
    assert "aggregation is NOT enabled" in messages
    assert "read-only" in messages.lower()


def test_detect_capabilities_matches_real_installed_types():
    caps = patch_mod._detect_capabilities()
    assert caps["handle_quote_importable"] is True
    assert caps["handle_quote_is_plain_python_method"] is True
    # Component._log is a compiled Cython attribute descriptor, not a
    # writable Python-level slot -- proven via the real type, not assumed.
    assert caps["component_log_descriptor"] is not None
    assert caps["logger_type_is_mutable"] is False


def test_genuine_polymarket_data_client_construction_succeeds():
    """The actual Gate 5 reproduction, in-process: builds a real
    PolymarketDataClient through TradingNode.build() -- the exact call
    chain that raised AttributeError under ea4c5a9 -- using the currently
    installed (repaired) patch chain. Construction-only: no node.run(), no
    network, no real credentials (see data_client_construction_smoke_test.py
    for the full safety rationale, shared here via import)."""
    node = smoke_test._build_minimal_node()
    node.build()  # must not raise -- this is the exact line that crashed under ea4c5a9

    from nautilus_trader.common.component import Logger

    data_engine = node.kernel.data_engine
    client_ids = data_engine.registered_clients
    assert client_ids, "expected at least one registered data client"
    client = data_engine._clients[client_ids[0]]

    assert isinstance(client._log, Logger)
    assert type(client._log).__module__ != "patch_quote_tick_logging"

    # Reproduce the ORIGINAL ea4c5a9 failure directly against this genuine,
    # fully-constructed instance: assigning _log must still raise the exact
    # AttributeError it always did -- proving apply_quote_tick_logging_patch()
    # simply never attempts this, rather than the attempt having become safe.
    with pytest.raises(AttributeError):
        client._log = "not a logger"
