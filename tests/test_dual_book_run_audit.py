"""
Regression tests for the full-runner dual-book audit + hardened preflight.
DRY-RUN / DIAGNOSTIC, deterministic, offline.
"""
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

import dual_book_run_audit as audit
import preflight_dryrun as pf


def _rec(side, **over):
    """A schema-v2 would-trade record with healthy dual-book fields."""
    yes_tok, no_tok = "YES1", "NO1"
    base = {
        "would_trade": True, "shadow_side": side, "config_name": "cfg",
        "timestamp": "2026-07-19T21:33:00+00:00",
        "market_id": f"0xcond-{yes_tok if side=='long' else no_tok}.POLYMARKET",
        "condition_id": "0xcond", "market_start_time": 1784496000.0,
        "market_end_time": 1784496900.0,
        "yes_token_id": yes_tok, "no_token_id": no_tok,
        "selected_token_id": yes_tok if side == "long" else no_tok,
        "yes_bid": 0.60, "yes_ask": 0.62, "yes_mid": 0.61,
        "no_bid": 0.37, "no_ask": 0.39, "no_mid": 0.38,
        "no_mid_source": "no_book",
        "yes_book_status": "ok", "no_book_status": "ok",
        "yes_quote_timestamp": 1784496780.0, "no_quote_timestamp": 1784496780.0,
        "yes_quote_age_s": 0.2, "no_quote_age_s": 0.2,
        "selected_quote_age_s": 0.2, "cross_book_skew_s": 0.0,
        "book_generation": 1,
        "selected_exec_price": 0.62 if side == "long" else 0.39,
        "selected_exec_price_source": "yes_ask" if side == "long" else "no_ask",
        "economic_pnl_unavailable": False,
        "paper_requested_stake": 1.0, "paper_requested_shares": 1.6,
        "paper_fill_status": "filled",
        "paper_filled_shares": 1.0 / (0.62 if side == "long" else 0.39),
        "paper_spent": 1.0, "paper_vwap": 0.62 if side == "long" else 0.39,
        "paper_unfilled_stake": 0.0,
        "paper_top_ask": 0.62 if side == "long" else 0.39,
        "paper_top_ask_size": 90.0,
        "paper_ask_levels_used": [[0.62 if side == "long" else 0.39, 1.6]],
        "paper_pre_fee": True, "paper_fee_rate": None,
        "paper_slippage_model": "walk_observed_ask_depth",
    }
    base.update(over)
    return base


def _official(cid, outcome):
    return {cid.lower(): {
        "status": "resolved", "condition_id": cid, "resolved_outcome": outcome,
        "winning_token_id": "W", "losing_token_id": "L",
        "parser_version": "clob-resolution-2", "retrieved_at": "2026-07-20T00:00:00Z",
    }}


def test_long_uses_yes_ask_all_proofs_pass():
    recs = [_rec("long")]
    built = audit.build(recs, official_cache=_official("0xcond", "UP"))
    row = built["rows"][0]
    assert row["selected_exec_price_source"] == "yes_ask"
    assert row["selected_exec_price"] == 0.62
    for col in [c for c in audit.FORENSIC_FIELDS if c.startswith("ok_")]:
        assert row[col] == 1, col
    assert row["win"] == 1  # long wins on UP


def test_short_uses_no_ask_never_yes_mid():
    recs = [_rec("short")]
    built = audit.build(recs, official_cache=_official("0xcond", "DOWN"))
    row = built["rows"][0]
    assert row["selected_token_id"] == "NO1"
    assert row["selected_exec_price_source"] == "no_ask"
    assert row["selected_exec_price"] == 0.39
    assert row["ok_not_yes_mid"] == 1
    assert row["ok_exec_from_selected_ask"] == 1
    assert row["win"] == 1  # short wins on DOWN


def test_short_priced_off_yes_mid_is_flagged():
    # A (hypothetical) contaminated record must fail the not-yes-mid proof.
    bad = _rec("short", selected_exec_price=0.61,
               selected_exec_price_source="derived_from_yes_mid")
    built = audit.build([bad], official_cache=_official("0xcond", "DOWN"))
    assert built["rows"][0]["ok_not_yes_mid"] == 0


def test_l1_depth_is_flagged_and_not_fabricated():
    built = audit.build([_rec("long")], official_cache=_official("0xcond", "UP"))
    row = built["rows"][0]
    assert row["depth_source"] == "nautilus_l1_top_of_book"
    assert row["depth_level_count"] == 1
    assert row["l1_limited"] == 1


def test_stake_exceeding_top_level_marks_partial():
    partial = _rec("long", paper_fill_status="partially_filled",
                   paper_unfilled_stake=0.5, paper_spent=0.5,
                   paper_filled_shares=0.8)
    built = audit.build([partial], official_cache=_official("0xcond", "UP"))
    row = built["rows"][0]
    assert row["stake_exceeded_top_level"] == 1
    assert row["fill_status"] == "partially_filled"


def test_quote_after_decision_fails_proof():
    late = _rec("long", yes_quote_timestamp=1784496900.0,  # after decision
                timestamp="2026-07-19T21:30:00+00:00")
    # decision epoch ~1784496600; quote at 1784496900 is >1s later
    built = audit.build([late], official_cache=_official("0xcond", "UP"))
    assert built["rows"][0]["ok_quote_before_decision"] == 0


def test_summary_views_and_availability():
    recs = [_rec("long"), _rec("long", config_name="cfg2")]
    built = audit.build(recs, official_cache=_official("0xcond", "UP"))
    summary = audit.summarize(built, recs)
    assert summary["would_trade_records"] == 2
    assert summary["book_availability_all_records"]["both"] == 2
    assert summary["view_c_unavailable_records"] == 0
    assert summary["proof_all_pass"] is True
    assert summary["official_validation_all_pass"] is True
    assert "long" in summary["fill_availability_by_side"]


def test_preflight_new_checks_present_and_pass():
    chk = pf.Check()
    pf._check_repo_and_venv(chk)
    pf._check_disk_space(chk)
    pf._check_clock_sync(chk)
    pf._check_dualbook_config(chk)
    pf._check_store_readable(chk)
    names = [r[0] for r in chk.rows]
    assert "repository layout" in names
    assert "disk space (logs partition)" in names
    assert "system clock NTP sync" in names
    assert "dual-book config present" in names
    assert "quote freshness/skew config" in names
    # none of the new checks should be a blocking FAIL in this repo
    assert not chk.blocking_fail
