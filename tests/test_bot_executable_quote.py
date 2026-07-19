"""
Bot executable-quote builder tests — DRY-RUN / DIAGNOSTIC ONLY.

Verifies `_build_executable_quote` records the contemporaneous YES book and
deterministically derives the identity/timing fields (condition_id from the
market id, market_end_time = slug start + 900s) WITHOUT any network call or
closed-market state, and never fabricates a NO-token ask.
"""
import sys
import types
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import bot as botmod


def _stub(instrument_id, yes_token_id="YES1", bid_ask=(0.60, 0.62)):
    s = types.SimpleNamespace()
    s._last_bid_ask = bid_ask
    s._yes_token_id = yes_token_id
    s.instrument_id = instrument_id
    return s


def test_quote_derives_condition_id_and_end_time():
    cid = "0x" + "a" * 64
    stub = _stub(f"{cid}-999.POLYMARKET")
    q = botmod.IntegratedBTCStrategy._build_executable_quote(
        stub, {"market_slug": "btc-updown-15m-1784439900"})
    assert q["condition_id"] == cid                 # parsed from market id
    assert q["market_start_time"] == 1784439900.0
    assert q["market_end_time"] == 1784440800.0     # start + 900s
    assert q["yes_bid"] == 0.60 and q["yes_ask"] == 0.62
    assert q["yes_token_id"] == "YES1"
    assert q["quote_source"] == "yes_book_ws"


def test_quote_never_fabricates_no_book():
    stub = _stub("0x" + "b" * 64 + "-1.POLYMARKET")
    q = botmod.IntegratedBTCStrategy._build_executable_quote(
        stub, {"market_slug": "btc-updown-15m-1784439900"})
    assert q["no_bid"] is None and q["no_ask"] is None
    assert q["no_token_id"] is None                 # NO book not subscribed


def test_quote_explicit_condition_id_wins_over_parse():
    stub = _stub("0x" + "c" * 64 + "-1.POLYMARKET")
    q = botmod.IntegratedBTCStrategy._build_executable_quote(
        stub, {"market_slug": "btc-updown-15m-1784439900",
               "condition_id": "0x" + "d" * 64})
    assert q["condition_id"] == "0x" + "d" * 64
