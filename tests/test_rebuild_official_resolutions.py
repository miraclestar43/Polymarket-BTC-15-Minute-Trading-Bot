"""
Safe official-resolution rebuild tests — DRY-RUN / DIAGNOSTIC, offline.

Verifies: agreeing rebuild replaces the active store; an outcome flip or a
network regression leaves the active store INTACT; and a backup is always made.
"""
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

import rebuild_official_resolutions as rb


def _seed_store(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    recs = [
        {"status": "resolved", "resolved_outcome": "UP",
         "condition_id": "0xa", "requested_condition_id": "0xa"},
        {"status": "resolved", "resolved_outcome": "DOWN",
         "condition_id": "0xb", "requested_condition_id": "0xb"},
    ]
    with open(log_dir / "official_resolutions.jsonl", "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def _payload(cid, up_winner):
    return {
        "condition_id": cid, "closed": True,
        "tokens": [
            {"token_id": "t1", "outcome": "Up", "winner": up_winner, "price": 1 if up_winner else 0},
            {"token_id": "t2", "outcome": "Down", "winner": not up_winner, "price": 0 if up_winner else 1},
        ],
    }


def test_agreeing_rebuild_replaces_store(tmp_path):
    logs = tmp_path / "logs"
    _seed_store(logs)
    fixtures = {"0xa": _payload("0xa", True), "0xb": _payload("0xb", False)}
    summary = rb.rebuild(log_dir=str(logs), fixtures=fixtures, apply=True)
    assert summary["validation_ok"] is True
    assert summary["active_store_replaced"] is True
    assert not summary["mismatches"] and not summary["regressions"]
    assert summary["backup"] is not None and Path(summary["backup"]).exists()


def test_outcome_flip_does_not_replace(tmp_path):
    logs = tmp_path / "logs"
    _seed_store(logs)
    # 0xa now returns DOWN (was UP) → mismatch → active store untouched.
    fixtures = {"0xa": _payload("0xa", False), "0xb": _payload("0xb", False)}
    summary = rb.rebuild(log_dir=str(logs), fixtures=fixtures, apply=True)
    assert summary["validation_ok"] is False
    assert summary["active_store_replaced"] is False
    assert summary["mismatches"] and summary["mismatches"][0]["condition_id"] == "0xa"
    # active store still has the ORIGINAL outcome
    store = rb._load_store(logs / "official_resolutions.jsonl")
    assert store["0xa"]["resolved_outcome"] == "UP"


def test_network_regression_leaves_store_intact(tmp_path):
    logs = tmp_path / "logs"
    _seed_store(logs)
    # 0xa fetch fails → regression (was resolved) → no replace.
    fixtures = {"0xa": "__network__", "0xb": _payload("0xb", False)}
    summary = rb.rebuild(log_dir=str(logs), fixtures=fixtures, apply=True)
    assert summary["validation_ok"] is False
    assert summary["active_store_replaced"] is False
    assert summary["regressions"]
    store = rb._load_store(logs / "official_resolutions.jsonl")
    assert store["0xa"]["resolved_outcome"] == "UP"


def test_report_only_never_replaces(tmp_path):
    logs = tmp_path / "logs"
    _seed_store(logs)
    fixtures = {"0xa": _payload("0xa", True), "0xb": _payload("0xb", False)}
    summary = rb.rebuild(log_dir=str(logs), fixtures=fixtures, apply=False)
    assert summary["validation_ok"] is True
    assert summary["active_store_replaced"] is False   # report-only
