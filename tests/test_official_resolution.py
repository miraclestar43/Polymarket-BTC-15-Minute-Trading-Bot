"""
Official-resolution retrieval tests (Part B) — DRY-RUN / DIAGNOSTIC ONLY.

Deterministic, offline (fixture opener; no network). Covers CLOB payload
parsing for resolved YES/NO/UP/DOWN, pending (not closed), closed-no-winner,
condition-id mismatch (the historical wrong-market bug), malformed payloads,
HTTP 404 -> not_found, network failure, missing identifier, the clean store
round-trip, and official/inferred source separation.
"""
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.strategy_brain import official_resolution as ores
from core.strategy_brain import settlement as st

_CID = "0x" + "a" * 64


def _clob_payload(cid=_CID, closed=True, winner_outcome="Up",
                  loser_outcome="Down"):
    return {
        "condition_id": cid,
        "question": "Bitcoin Up or Down?",
        "market_slug": "btc-updown-15m-1782437400",
        "closed": closed,
        "tokens": [
            {"token_id": "t_up", "outcome": winner_outcome, "price": 1, "winner": True},
            {"token_id": "t_dn", "outcome": loser_outcome, "price": 0, "winner": False},
        ],
    }


# --------------------------------------------------------------------------
# Pure parsing
# --------------------------------------------------------------------------
def test_parse_resolved_up():
    r = ores.parse_clob_market(_clob_payload(winner_outcome="Up"), _CID)
    assert r["status"] == ores.STATUS_RESOLVED
    assert r["resolved_outcome"] == "UP"
    assert r["winning_token_id"] == "t_up"


def test_parse_resolved_down_and_yes_no_mapping():
    r = ores.parse_clob_market(_clob_payload(winner_outcome="Down"), _CID)
    assert r["resolved_outcome"] == "DOWN"
    y = ores.parse_clob_market(_clob_payload(winner_outcome="Yes"), _CID)
    assert y["resolved_outcome"] == "UP"      # YES -> UP
    n = ores.parse_clob_market(_clob_payload(winner_outcome="No"), _CID)
    assert n["resolved_outcome"] == "DOWN"    # NO -> DOWN


def test_parse_pending_when_not_closed():
    r = ores.parse_clob_market(_clob_payload(closed=False), _CID)
    assert r["status"] == ores.STATUS_PENDING
    assert r["resolved_outcome"] is None


def test_parse_condition_id_mismatch_is_not_trusted():
    # returned market is a DIFFERENT market (the historical wrong-market bug)
    r = ores.parse_clob_market(_clob_payload(cid="0x" + "b" * 64), _CID)
    assert r["status"] == ores.STATUS_NOT_FOUND
    assert r["parse_error"] == "condition_id_mismatch"
    assert r["resolved_outcome"] is None


def test_parse_closed_no_winner_does_not_fabricate():
    payload = _clob_payload()
    for t in payload["tokens"]:
        t["winner"] = False
        t["price"] = 0.5
    r = ores.parse_clob_market(payload, _CID)
    assert r["resolved_outcome"] is None
    assert r["status"] == ores.STATUS_PENDING


def test_parse_non_dict_payload():
    r = ores.parse_clob_market(["not", "a", "dict"], _CID)
    assert r["status"] == ores.STATUS_MALFORMED
    assert r["parse_error"] == "payload_not_object"


def test_parse_captures_both_token_ids_and_labels():
    r = ores.parse_clob_market(_clob_payload(winner_outcome="Up"), _CID)
    assert r["winning_token_id"] == "t_up"
    assert r["losing_token_id"] == "t_dn"          # both sides identified
    assert r["winning_price"] == 1
    assert r["winning_outcome_raw"] == "Up"
    assert r["parser_version"] == ores.PARSER_VERSION
    assert isinstance(r["tokens_summary"], list) and len(r["tokens_summary"]) == 2


def test_parse_rejects_ambiguous_multiple_winners():
    payload = _clob_payload()
    for t in payload["tokens"]:          # both flagged winner
        t["winner"] = True
        t["price"] = 1
    r = ores.parse_clob_market(payload, _CID)
    assert r["status"] == ores.STATUS_MALFORMED
    assert r["parse_error"] == "closed_multiple_winners"
    assert r["resolved_outcome"] is None


def test_parse_price_one_alone_without_outcome_is_rejected():
    # A token at price==1 but with an unrecognizable outcome label must not be
    # trusted on price alone (winner/outcome identity is ambiguous).
    payload = {
        "condition_id": _CID, "closed": True,
        "tokens": [
            {"token_id": "t_x", "outcome": "Maybe", "price": 1, "winner": None},
            {"token_id": "t_y", "outcome": "Other", "price": 0, "winner": None},
        ],
    }
    r = ores.parse_clob_market(payload, _CID)
    assert r["status"] == ores.STATUS_MALFORMED
    assert r["resolved_outcome"] is None
    assert str(r["parse_error"]).startswith("unrecognized_outcome")


def test_parse_price_one_fallback_when_no_winner_flag():
    # No winner flag but exactly one token at price==1 with a valid label -> ok.
    payload = {
        "condition_id": _CID, "closed": True,
        "tokens": [
            {"token_id": "t_up", "outcome": "Up", "price": 1, "winner": None},
            {"token_id": "t_dn", "outcome": "Down", "price": 0, "winner": None},
        ],
    }
    r = ores.parse_clob_market(payload, _CID)
    assert r["status"] == ores.STATUS_RESOLVED
    assert r["resolved_outcome"] == "UP" and r["winning_token_id"] == "t_up"


# --------------------------------------------------------------------------
# fetch_official via fixture opener (no network)
# --------------------------------------------------------------------------
def test_fetch_resolved_via_fixture():
    opener = ores.make_fixture_opener({_CID: _clob_payload()})
    r = ores.fetch_official(_CID, opener=opener)
    assert r["status"] == ores.STATUS_RESOLVED and r["resolved_outcome"] == "UP"
    assert r["request_url"].endswith(_CID)


def test_fetch_http_404_is_not_found():
    opener = ores.make_fixture_opener({_CID: "__http_404__"})
    r = ores.fetch_official(_CID, opener=opener)
    assert r["status"] == ores.STATUS_NOT_FOUND


def test_fetch_network_failure():
    opener = ores.make_fixture_opener({_CID: "__network__"})
    r = ores.fetch_official(_CID, opener=opener)
    assert r["status"] == ores.STATUS_NETWORK_FAILURE


def test_fetch_malformed_json():
    opener = ores.make_fixture_opener({_CID: "__malformed__"})
    r = ores.fetch_official(_CID, opener=opener)
    assert r["status"] == ores.STATUS_MALFORMED


def test_fetch_missing_identifier():
    r = ores.fetch_official(None)
    assert r["status"] == ores.STATUS_IDENTIFIER_UNAVAILABLE
    assert r["parse_error"] == "no_condition_id"


def test_fetch_unknown_condition_id_returns_not_found():
    opener = ores.make_fixture_opener({})  # empty -> opener raises 404
    r = ores.fetch_official(_CID, opener=opener)
    assert r["status"] == ores.STATUS_NOT_FOUND


# --------------------------------------------------------------------------
# Clean store round-trip + official lookup
# --------------------------------------------------------------------------
def test_store_roundtrip_and_official_lookup(tmp_path):
    store_file = tmp_path / ores.OFFICIAL_STORE_FILE
    rec = ores.fetch_official(_CID, opener=ores.make_fixture_opener({_CID: _clob_payload()}))
    store_file.write_text(json.dumps(rec) + "\n")

    store = ores.load_official_store(str(tmp_path))
    assert ores.official_outcome_for(_CID, store) == "UP"
    assert ores.official_outcome_for(_CID.upper(), store) == "UP"   # case-insensitive
    assert ores.official_outcome_for("0x" + "c" * 64, store) is None


def test_official_lookup_ignores_unresolved(tmp_path):
    pending = ores.parse_clob_market(_clob_payload(closed=False), _CID)
    pending["requested_condition_id"] = _CID
    (tmp_path / ores.OFFICIAL_STORE_FILE).write_text(json.dumps(pending) + "\n")
    store = ores.load_official_store(str(tmp_path))
    assert ores.official_outcome_for(_CID, store) is None


# --------------------------------------------------------------------------
# Settlement wiring: official store preferred, sources never silently mixed
# --------------------------------------------------------------------------
def test_settlement_official_from_store(tmp_path):
    rec = ores.fetch_official(_CID, opener=ores.make_fixture_opener({_CID: _clob_payload()}))
    (tmp_path / ores.OFFICIAL_STORE_FILE).write_text(json.dumps(rec) + "\n")
    official_store = st.load_official_resolutions(str(tmp_path))
    identity = {"condition_id": _CID}
    assert st.official_from_store(identity, official_store) == "UP"
    # empty store -> no official outcome (never fabricated)
    assert st.official_from_store(identity, {}) is None
