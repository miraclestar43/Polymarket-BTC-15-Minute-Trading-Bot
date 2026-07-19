"""
Read-only CLOB/Gamma client + smoke-test tests — DRY-RUN, offline (fixtures).

Verifies market discovery maps Up/Down outcome labels to the correct token
ids, /book parsing, and that the smoke test proves a real NO ask is captured —
all without touching the network.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

from core.market_data import clob_client as cc
import no_book_smoke_test as smoke


def _fixtures(slug):
    market = [{
        "conditionId": "0xcond",
        "clobTokenIds": '["YESTOKEN", "NOTOKEN"]',
        "outcomes": '["Up", "Down"]',
        "question": "BTC up or down?",
        "slug": slug,
        "closed": False,
    }]
    return {
        f"slug={slug}": market,
        "token_id=YESTOKEN": {"bids": [{"price": "0.60", "size": "100"}],
                              "asks": [{"price": "0.62", "size": "80"}],
                              "timestamp": "1000000"},
        "token_id=NOTOKEN": {"bids": [{"price": "0.37", "size": "50"}],
                             "asks": [{"price": "0.39", "size": "40"}],
                             "timestamp": "1000000"},
    }


def test_discovery_maps_outcomes_to_tokens():
    slug = cc.current_btc_15m_slug(now=1000)
    opener = cc.make_fixture_opener(_fixtures(slug))
    d = cc.discover_current_market(now=1000, opener=opener)
    assert d["yes_token_id"] == "YESTOKEN"    # Up
    assert d["no_token_id"] == "NOTOKEN"      # Down
    assert d["condition_id"] == "0xcond"


def test_fetch_book_parses_levels():
    slug = cc.current_btc_15m_slug(now=1000)
    opener = cc.make_fixture_opener(_fixtures(slug))
    b = cc.fetch_book("NOTOKEN", opener=opener)
    assert b["asks"] == [(0.39, 40.0)]
    assert b["bids"] == [(0.37, 50.0)]


def test_smoke_test_captures_real_no_ask():
    slug = cc.current_btc_15m_slug(now=1000)
    fx = _fixtures(slug)
    summary = smoke.run_smoke(
        fixtures=fx, once=True, now_fn=lambda: 1000.0)
    assert summary["result"] == "PASS"
    assert summary["captured_executable_no_ask"] == 0.39   # real NO ask


def test_smoke_test_fails_when_market_missing():
    summary = smoke.run_smoke(fixtures={}, once=True, now_fn=lambda: 1000.0)
    assert summary["result"] == "FAIL"
