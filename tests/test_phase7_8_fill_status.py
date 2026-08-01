"""
scripts/phase7_8_analysis.py fill_status population — regression tests,
DRY-RUN / DIAGNOSTIC ONLY, deterministic, offline.

Root cause this pins down: an earlier ad-hoc copy of this analysis (used
for the Week-1/Week-2 closeouts) filtered the executable-fill population
with ``fill_status in ("filled", "partial")``, but paper_execution.py only
ever writes ``"filled"``, ``"partially_filled"``, or ``"unavailable"`` --
"partial" never matches "partially_filled", so every partial-fill record
was silently excluded from the fee-sensitivity and paired-comparison
population. scripts/phase7_8_analysis.py fixes this by importing the real
constants from core.strategy_brain.paper_execution instead of re-typing
the string.
"""
import csv
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

from core.strategy_brain import paper_execution as pe  # noqa: E402
import phase7_8_analysis as p78  # noqa: E402


def _row(**over):
    """A minimal, valid would-trade forensic-CSV row (all-string values,
    matching what csv.DictReader actually produces)."""
    base = {
        "timestamp": "2026-07-27T06:00:00.000000+00:00",
        "config_name": "baseline",
        "condition_id": "0xabc",
        "shadow_side": "long",
        "selected_exec_price": "0.60",
        "resolution_status": "resolved",
        "win": "1",
        "fill_status": pe.FILLED,
        "spent": "1.0",
        "filled_shares": "1.6667",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# 1. filled rows remain included
# ---------------------------------------------------------------------------
def test_filled_row_is_included():
    assert p78.is_valid_executable(_row(fill_status=pe.FILLED)) is True


# ---------------------------------------------------------------------------
# 2. partially_filled rows are included where intended (THE BUG)
# ---------------------------------------------------------------------------
def test_partially_filled_row_is_included():
    row = _row(fill_status=pe.PARTIALLY_FILLED, spent="0.34", filled_shares="0.34")
    assert p78.is_valid_executable(row) is True


def test_the_old_wrong_string_would_have_excluded_it():
    # Documents exactly why the bug existed: the literal string "partial"
    # the old code checked for is NOT the canonical value.
    row = _row(fill_status=pe.PARTIALLY_FILLED)
    assert row["fill_status"] == "partially_filled"
    assert row["fill_status"] != "partial"
    assert "partial" not in ("filled",)  # the old (buggy) tuple never matched it
    assert p78.is_valid_executable(row) is True  # the FIXED code does match it


# ---------------------------------------------------------------------------
# 3. unavailable / settlement-pending rows retain their intended treatment
# ---------------------------------------------------------------------------
def test_unavailable_row_is_excluded_from_economic_population():
    row = _row(fill_status=pe.UNAVAILABLE, selected_exec_price="", spent="")
    assert p78.is_valid_executable(row) is False


def test_pending_settlement_row_is_excluded_from_economic_population():
    row = _row(fill_status=pe.FILLED, resolution_status="pending", win="")
    assert p78.is_valid_executable(row) is False


def test_unknown_side_row_is_excluded():
    row = _row(shadow_side="unknown", selected_exec_price="", fill_status="n/a_no_trade")
    assert p78.is_valid_executable(row) is False


# ---------------------------------------------------------------------------
# 4. end-to-end: a small CSV mirroring the Week-2 shape is no longer
# silently short by its partial-fill rows.
# ---------------------------------------------------------------------------
_FIELDS = [
    "timestamp", "config_name", "condition_id", "shadow_side",
    "selected_exec_price", "resolution_status", "win", "spent",
    "filled_shares", "fill_status",
]


def _write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in _FIELDS})


def test_end_to_end_partial_fills_are_no_longer_silently_omitted(tmp_path):
    rows = [
        _row(config_name="baseline", condition_id=f"0x{i}", fill_status=pe.FILLED)
        for i in range(3)
    ] + [
        # exactly the Week-2 shape: 4 partial-fill rows for one variant
        _row(config_name="shadow_no_kelly", condition_id=f"0xp{i}",
             fill_status=pe.PARTIALLY_FILLED, spent="0.5", filled_shares="0.5")
        for i in range(4)
    ] + [
        _row(config_name="shadow_no_kelly", condition_id="0xu1",
             fill_status=pe.UNAVAILABLE, selected_exec_price="", spent=""),
        _row(config_name="shadow_no_kelly", condition_id="0xpend",
             fill_status=pe.FILLED, resolution_status="pending", win=""),
    ]
    csv_path = tmp_path / "forensic.csv"
    _write_csv(csv_path, rows)

    result = p78.run_analysis(
        csv_path=csv_path,
        run_start=p78._parse_iso("2026-07-27T00:00:00Z"),
        freeze_ts=p78._parse_iso("2026-08-01T00:00:00Z"),
        baseline_name="baseline",
        largest_variant_name="shadow_no_kelly",
    )
    fee0 = result["fee_sensitivity_hypothetical"]["shadow_no_kelly"]["fee_0.0000"]
    # 4 partial fills must be counted -- this is exactly what the bug dropped.
    assert fee0["n"] == 4, f"expected the 4 partial-fill rows to be counted, got n={fee0['n']}"
    assert result["n_valid_executable_total"] == 3 + 4  # baseline(3 filled) + shadow_no_kelly(4 partial)


# NOTE: a cross-check against the real, preserved Week-2 forensic CSV
# (n=316, net_pnl=+1.9519, ROR=0.62% for shadow_no_kelly at 0% fee) is
# intentionally NOT a test here -- it depends on machine-specific state
# (/home/star/polymarket-w2-final-audit-20260801/, present only on the host
# that ran that audit), which is not something a portable repository test
# suite should require or silently skip on. That cross-check is instead a
# one-off, read-only audit command, run manually against a temporary
# read-only copy of the artifact, e.g.:
#
#   .venv/bin/python -c "
#   import shutil, tempfile, sys
#   from pathlib import Path
#   sys.path.insert(0, 'scripts')
#   import phase7_8_analysis as p78
#   src = Path('/home/star/polymarket-w2-final-audit-20260801/derived/w2_final_forensic.csv')
#   with tempfile.TemporaryDirectory() as td:
#       ro = Path(td) / 'ro_copy.csv'
#       shutil.copyfile(src, ro); ro.chmod(0o444)
#       r = p78.run_analysis(ro, p78._parse_iso('2026-07-27T05:13:37.827453Z'),
#                             p78._parse_iso('2026-08-01T18:28:02.228633Z'))
#       print(r['fee_sensitivity_hypothetical']['shadow_no_kelly']['fee_0.0000'])
#   "
#
# The four-partial-fill regression proof itself remains fully covered above
# with deterministic, self-contained fixture data.
