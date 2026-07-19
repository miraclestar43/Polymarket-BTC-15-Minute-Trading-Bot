"""
Dual-token order-book store tests — DRY-RUN / DIAGNOSTIC, deterministic, offline.

Covers: both tokens subscribed, snapshot routed by token id, incremental
top-of-book routed by token id, reconnect resubscribes/invalidates, stale
pre-reconnect data invalidated, cross-market contamination rejected,
crossed/out-of-range books rejected, cross-book skew recorded, and safe market
replacement.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.market_data.dual_book import (
    DualBookStore, STATUS_OK, STATUS_STALE, STATUS_CROSSED,
    STATUS_OUT_OF_RANGE, STATUS_MISSING,
)


def _store():
    s = DualBookStore(freshness_seconds=60.0, skew_warn_seconds=5.0)
    s.set_market("0xcond", "YES1", "NO1", "mkt")
    return s


def test_both_tokens_tracked_separately():
    s = _store()
    assert s.yes_token_id == "YES1" and s.no_token_id == "NO1"
    assert s.yes.token_id == "YES1" and s.no.token_id == "NO1"


def test_snapshot_routed_by_token_id():
    s = _store()
    ok_yes = s.apply_snapshot("YES1", [(0.60, 100)], [(0.62, 80)], ts=1000.0)
    ok_no = s.apply_snapshot("NO1", [(0.37, 50)], [(0.39, 40)], ts=1000.0)
    assert ok_yes and ok_no
    assert s.yes.best_ask == 0.62 and s.yes.best_ask_size == 80
    assert s.no.best_ask == 0.39 and s.no.best_ask_size == 40


def test_top_of_book_routed_by_token_id():
    s = _store()
    assert s.apply_top_of_book("NO1", 0.37, 50, 0.39, 40, ts=1000.0)
    snap = s.snapshot_quote(now=1000.0)
    assert snap["no_ask"] == 0.39 and snap["no_ask_size"] == 40
    assert snap["no_ask_ladder"] == [(0.39, 40.0)]


def test_unknown_token_rejected_no_contamination():
    s = _store()
    # A snapshot for a token from a DIFFERENT market must be rejected.
    assert s.apply_snapshot("OTHER_MKT_TOKEN", [(0.5, 1)], [(0.5, 1)], ts=1000.0) is False
    assert s.yes.best_ask is None and s.no.best_ask is None


def test_stale_book_not_executable():
    s = _store()
    s.apply_snapshot("NO1", [(0.37, 50)], [(0.39, 40)], ts=1000.0)
    # 61s later the book is older than the 60s freshness threshold.
    snap = s.snapshot_quote(now=1061.0)
    assert snap["no_book_status"] == STATUS_STALE
    assert snap["no_ask"] is None                    # not executable
    assert snap["no_observed_ask"] == 0.39           # still recorded for audit


def test_crossed_book_not_executable():
    s = _store()
    s.apply_snapshot("NO1", [(0.50, 10)], [(0.49, 10)], ts=1000.0)  # bid>ask
    snap = s.snapshot_quote(now=1000.0)
    assert snap["no_book_status"] == STATUS_CROSSED
    assert snap["no_ask"] is None
    assert snap["validation_flags"]["no_bid_le_ask"] is False


def test_out_of_range_book_not_executable():
    s = _store()
    s.apply_snapshot("YES1", [(0.60, 10)], [(1.20, 10)], ts=1000.0)  # ask>1
    snap = s.snapshot_quote(now=1000.0)
    assert snap["yes_book_status"] == STATUS_OUT_OF_RANGE
    assert snap["yes_ask"] is None
    assert snap["validation_flags"]["prices_in_range"] is False


def test_reconnect_invalidates_pre_reconnect_data():
    s = _store()
    s.apply_snapshot("YES1", [(0.60, 10)], [(0.62, 10)], ts=1000.0)
    s.apply_snapshot("NO1", [(0.37, 10)], [(0.39, 10)], ts=1000.0)
    assert s.snapshot_quote(now=1000.0)["yes_book_status"] == STATUS_OK
    # Disconnect: both books become non-executable until a fresh snapshot.
    s.on_disconnect()
    s.on_resubscribe()
    snap = s.snapshot_quote(now=1000.0)
    assert snap["yes_book_status"] == STATUS_MISSING
    assert snap["no_book_status"] == STATUS_MISSING
    # Fresh snapshot after resubscribe restores executability.
    s.apply_snapshot("YES1", [(0.61, 10)], [(0.63, 10)], ts=1001.0)
    assert s.snapshot_quote(now=1001.0)["yes_book_status"] == STATUS_OK


def test_market_switch_replaces_expired_safely():
    s = _store()
    s.apply_snapshot("YES1", [(0.60, 10)], [(0.62, 10)], ts=1000.0)
    gen0 = s.generation
    changed = s.set_market("0xcond2", "YES2", "NO2", "mkt2")
    assert changed and s.generation == gen0 + 1
    # Old books cleared; a stale YES1 snapshot no longer routes anywhere.
    assert s.apply_snapshot("YES1", [(0.9, 1)], [(0.95, 1)], ts=1000.0) is False
    assert s.snapshot_quote(now=1000.0)["yes_ask"] is None


def test_cross_book_skew_recorded():
    s = _store()
    s.apply_snapshot("YES1", [(0.60, 10)], [(0.62, 10)], ts=1000.0)
    s.apply_snapshot("NO1", [(0.37, 10)], [(0.39, 10)], ts=1007.0)  # 7s later
    snap = s.snapshot_quote(now=1007.0)
    assert snap["cross_book_skew_s"] == 7.0
    assert snap["validation_flags"]["cross_book_skew_ok"] is False  # > 5s warn


def test_selected_ask_ladder_only_when_ok():
    s = _store()
    s.apply_snapshot("NO1", [(0.37, 10)], [(0.39, 40), (0.41, 60)], ts=1000.0)
    assert s.selected_ask_ladder("short", now=1000.0) == [(0.39, 40.0), (0.41, 60.0)]
    # Stale → no executable ladder.
    assert s.selected_ask_ladder("short", now=1100.0) is None
