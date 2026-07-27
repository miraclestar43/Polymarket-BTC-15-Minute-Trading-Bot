"""
Run provenance — DIAGNOSTIC / METADATA ONLY. Deterministic, offline tests.

Covers: Git HEAD/dirty-state recording, config hashing, no-secret inclusion,
run_id propagation via env vars, atomic manifest writing, unavailable-Git
fallback, and start/end manifest lifecycle. See core/diagnostics/run_provenance.py
for why this exists (the 2026-07-20..27 run had source files change while
the runner was still active between child restarts).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.diagnostics import run_provenance as rp  # noqa: E402


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo)] + list(args), check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _init_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("v1\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


# ---------------------------------------------------------------------------
# Git HEAD / dirty-state recording
# ---------------------------------------------------------------------------
def test_git_head_recorded_in_real_repo():
    info = rp.compute_git_info(project_root)
    assert info["head"] != rp.UNAVAILABLE
    assert len(info["head"]) == 40  # full SHA1
    assert all(c in "0123456789abcdef" for c in info["head"])


def test_dirty_flag_false_on_clean_repo(tmp_path):
    repo = _init_repo(tmp_path)
    info = rp.compute_git_info(repo)
    assert info["dirty"] is False


def test_dirty_flag_true_after_uncommitted_change(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "f.txt").write_text("v2 -- uncommitted\n")
    info = rp.compute_git_info(repo)
    assert info["dirty"] is True


def test_git_info_unavailable_outside_a_repo(tmp_path):
    not_a_repo = tmp_path / "plain_dir"
    not_a_repo.mkdir()
    info = rp.compute_git_info(not_a_repo)
    assert info == {"head": rp.UNAVAILABLE, "branch": rp.UNAVAILABLE, "dirty": rp.UNAVAILABLE}


# ---------------------------------------------------------------------------
# Config hashing
# ---------------------------------------------------------------------------
def test_sha256_file_matches_manual_hash(tmp_path):
    import hashlib
    f = tmp_path / "strategy.yaml"
    f.write_text("min_prob: 0.87\n")
    expected = hashlib.sha256(f.read_bytes()).hexdigest()
    assert rp.sha256_file(f) == expected


def test_sha256_file_missing_returns_none(tmp_path):
    assert rp.sha256_file(tmp_path / "does_not_exist.yaml") is None


def test_sha256_changes_when_content_changes(tmp_path):
    f = tmp_path / "strategy.yaml"
    f.write_text("min_prob: 0.87\n")
    h1 = rp.sha256_file(f)
    f.write_text("min_prob: 0.90\n")
    h2 = rp.sha256_file(f)
    assert h1 != h2


# ---------------------------------------------------------------------------
# run_id
# ---------------------------------------------------------------------------
def test_run_id_deterministic_for_same_inputs():
    assert rp.make_run_id(1784521030.9, 19538) == rp.make_run_id(1784521030.9, 19538)


def test_run_id_differs_for_different_pid_or_time():
    a = rp.make_run_id(1784521030.9, 19538)
    b = rp.make_run_id(1784521030.9, 19999)
    c = rp.make_run_id(1784521999.0, 19538)
    assert a != b != c


# ---------------------------------------------------------------------------
# No-secret inclusion
# ---------------------------------------------------------------------------
def test_provenance_never_includes_a_secret_env_value(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PK", "sk-totally-secret-value-12345")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "another-secret-abcxyz")
    repo = _init_repo(tmp_path)
    (repo / "strategy.yaml").write_text("min_prob: 0.87\n")
    (repo / "requirements.txt").write_text("nautilus_trader==1.0\n")
    prov = rp.compute_provenance(
        repo_root=repo, bot_args=[], dry_run=True, live_trading_ack=False,
        runner_pid=123, runner_start_ts=1784521030.9, schema_version=2,
    )
    dumped = json.dumps(prov)
    assert "sk-totally-secret-value-12345" not in dumped
    assert "another-secret-abcxyz" not in dumped
    # Only derived metadata -- no raw env dump.
    assert set(prov.keys()) == {
        "run_id", "runner_start", "runner_pid", "git", "strategy_config_sha256",
        "requirements_sha256", "dependency_lock_source", "python_version",
        "python_executable", "schema_version", "app_version", "bot_args",
        "dry_run", "live_trading_ack", "hostname", "platform",
    }


def test_env_for_child_never_includes_a_secret_env_value(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYMARKET_PK", "sk-totally-secret-value-12345")
    repo = _init_repo(tmp_path)
    prov = rp.compute_provenance(
        repo_root=repo, bot_args=[], dry_run=True, live_trading_ack=False,
        runner_pid=123, runner_start_ts=1784521030.9,
    )
    overlay = rp.env_for_child(prov)
    assert "sk-totally-secret-value-12345" not in json.dumps(overlay)
    assert all(k.startswith(rp.ENV_PREFIX) for k in overlay)


# ---------------------------------------------------------------------------
# run_id / provenance propagation via env vars (runner -> child)
# ---------------------------------------------------------------------------
def test_child_reads_back_the_same_provenance_the_runner_computed(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    (repo / "strategy.yaml").write_text("min_prob: 0.87\n")
    prov = rp.compute_provenance(
        repo_root=repo, bot_args=[], dry_run=True, live_trading_ack=False,
        runner_pid=456, runner_start_ts=1784521030.9,
    )
    overlay = rp.env_for_child(prov)
    for k, v in overlay.items():
        monkeypatch.setenv(k, v)
    rp.reset_cache_for_tests()
    child_view = rp.get_current_run_provenance()
    assert child_view["run_id"] == prov["run_id"]
    assert child_view["git_head"] == prov["git"]["head"]
    assert child_view["git_dirty"] == prov["git"]["dirty"]
    assert child_view["strategy_config_sha256"] == prov["strategy_config_sha256"]
    rp.reset_cache_for_tests()


def test_child_provenance_caches_after_first_call(monkeypatch):
    monkeypatch.setenv(f"{rp.ENV_PREFIX}RUN_ID", "run_A")
    rp.reset_cache_for_tests()
    first = rp.get_current_run_provenance()
    # Change the env after the first read -- cached value must NOT change,
    # proving this is computed/read once per process, not per record.
    monkeypatch.setenv(f"{rp.ENV_PREFIX}RUN_ID", "run_B")
    second = rp.get_current_run_provenance()
    assert first["run_id"] == second["run_id"] == "run_A"
    rp.reset_cache_for_tests()


# ---------------------------------------------------------------------------
# Unknown Git metadata fallback (no crash)
# ---------------------------------------------------------------------------
def test_get_current_run_provenance_falls_back_when_env_missing(monkeypatch):
    for key in ("RUN_ID", "GIT_HEAD", "GIT_BRANCH", "GIT_DIRTY", "STRATEGY_CONFIG_SHA256"):
        monkeypatch.delenv(f"{rp.ENV_PREFIX}{key}", raising=False)
    rp.reset_cache_for_tests()
    view = rp.get_current_run_provenance()
    assert view["run_id"] == rp.UNAVAILABLE
    assert view["git_head"] == rp.UNAVAILABLE
    assert view["git_dirty"] == rp.UNAVAILABLE
    rp.reset_cache_for_tests()


def test_compute_provenance_never_raises_when_git_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "_run_git", lambda *a, **k: None)
    prov = rp.compute_provenance(
        repo_root=tmp_path, bot_args=[], dry_run=True, live_trading_ack=False,
        runner_pid=1, runner_start_ts=1784521030.9,
    )
    assert prov["git"]["head"] == rp.UNAVAILABLE


# ---------------------------------------------------------------------------
# Atomic manifest writing + start/end lifecycle
# ---------------------------------------------------------------------------
def test_write_json_atomic_readback(tmp_path):
    target = tmp_path / "sub" / "manifest.json"
    rp.write_json_atomic(target, {"a": 1, "b": "two"})
    assert json.loads(target.read_text()) == {"a": 1, "b": "two"}
    # no leftover temp files
    assert list(target.parent.glob(".manifest.json.*.tmp")) == []


def test_write_json_atomic_no_partial_file_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "manifest.json"

    class Boom(Exception):
        pass

    def _boom(*a, **k):
        raise Boom("fail mid-write")

    monkeypatch.setattr(json, "dump", _boom)
    with pytest.raises(Boom):
        rp.write_json_atomic(target, {"a": 1})
    assert not target.exists()
    assert list(tmp_path.glob(".manifest.json.*.tmp")) == []


def test_start_and_end_manifest_lifecycle(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    prov = {
        "run_id": "run_1784521030_999", "git": {"head": "abc", "branch": "main", "dirty": False},
        "strategy_config_sha256": "deadbeef",
    }
    start_path = rp.write_start_manifest(repo, prov)
    assert start_path.exists()
    assert json.loads(start_path.read_text())["run_id"] == "run_1784521030_999"

    end_path = rp.write_end_manifest(
        repo_root=repo, run_id=prov["run_id"], stop_reason="SIGTERM",
        child_start_count=5, child_exit_count=5, clean_exit_count=4,
        watchdog_restart_count=1, runner_start_ts=1784521030.9,
    )
    assert end_path.exists()
    end = json.loads(end_path.read_text())
    assert end["run_id"] == prov["run_id"]
    assert end["stop_reason"] == "SIGTERM"
    assert end["child_start_count"] == 5
    assert end["clean_exit_count"] == 4
    assert end["watchdog_restart_count"] == 1
    # start and end manifests are separate, both immutable/present
    assert start_path != end_path
    assert start_path.exists() and end_path.exists()


# ---------------------------------------------------------------------------
# Shadow-record propagation + unchanged dry-run safety gates
# ---------------------------------------------------------------------------
def test_shadow_records_carry_run_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv(f"{rp.ENV_PREFIX}RUN_ID", "run_test_123")
    monkeypatch.setenv(f"{rp.ENV_PREFIX}GIT_HEAD", "cafef00d" * 5)
    monkeypatch.setenv(f"{rp.ENV_PREFIX}GIT_DIRTY", "false")
    monkeypatch.setenv(f"{rp.ENV_PREFIX}STRATEGY_CONFIG_SHA256", "deadbeef")
    rp.reset_cache_for_tests()

    from core.strategy_brain import shadow_evaluator as se
    records = se.evaluate_shadow(
        log_dir=str(tmp_path),
        actual_event_type="evaluation", actual_decision_reason="test",
        timestamp="2026-06-23T12:00:00+00:00",
        market_id="0x" + "a" * 64 + "-1.POLYMARKET", direction="long",
        entry_price=0.70, p_model=0.90, p_model_source="markov", odds_b=0.43,
        kelly_raw=0.03, kelly_capped=0.03, kelly_size=5.0, markov_state="UP",
        p_stay=0.90, min_prob_actual=0.87, markov_window_actual=20,
        markov_passed=True, kelly_passed=True,
    )
    assert records
    for r in records:
        assert r["run_id"] == "run_test_123"
        assert r["git_head"] == "cafef00d" * 5
        assert r["git_dirty"] is False
        assert r["strategy_config_sha256"] == "deadbeef"
        # provenance is metadata only -- it must never change a decision.
        assert r["schema_version"] == se.SHADOW_SCHEMA_VERSION
        assert r["dry_run"] is True and r["simulation"] is True
    rp.reset_cache_for_tests()


# ---------------------------------------------------------------------------
# Aborted-pilot manifest classification (2026-07-27, isolation hardening)
# ---------------------------------------------------------------------------
def test_write_abort_manifest_classification(tmp_path):
    path = rp.write_abort_manifest(
        repo_root=tmp_path, run_id="run_1785102520_55073",
        status="aborted_isolation_pilot",
        reason="competing_week1_runner_and_shared_heartbeat_watchdog_masking",
        reason_detail="ran alongside the still-active Week-1 runner",
        start_timestamp="2026-07-26T21:48:40.853887+00:00",
        stop_timestamp="2026-07-27T03:53:46.619962+00:00",
        git_head="6106051c7d8385203fe3b719f1d07655470e948c",
        record_count=360,
    )
    assert path == rp.run_abort_manifest_path(tmp_path, "run_1785102520_55073")
    data = json.loads(path.read_text())
    assert data["run_id"] == "run_1785102520_55073"
    assert data["status"] == "aborted_isolation_pilot"
    assert data["reason"] == "competing_week1_runner_and_shared_heartbeat_watchdog_masking"
    assert data["shadow_decision_record_count"] == 360
    assert data["excluded_from_replication_metrics"] is True
    assert "classified_at" in data


def test_abort_manifest_does_not_touch_start_or_end_manifest(tmp_path):
    prov = {
        "run_id": "run_X", "git": {"head": "abc", "branch": "main", "dirty": False},
        "strategy_config_sha256": "deadbeef",
    }
    start_path = rp.write_start_manifest(tmp_path, prov)
    start_before = start_path.read_text()
    rp.write_abort_manifest(
        repo_root=tmp_path, run_id="run_X", status="aborted_isolation_pilot",
        reason="test", reason_detail="test", start_timestamp="t0",
        stop_timestamp="t1", git_head="abc", record_count=0,
    )
    assert start_path.read_text() == start_before  # unchanged, separate file
    assert not rp.run_manifest_end_path(tmp_path, "run_X").exists()


def test_abort_manifest_excluded_flag_can_be_set_false(tmp_path):
    path = rp.write_abort_manifest(
        repo_root=tmp_path, run_id="run_Y", status="aborted_for_other_reason",
        reason="test", reason_detail="test", start_timestamp="t0",
        stop_timestamp="t1", git_head="abc", record_count=0,
        excluded_from_replication_metrics=False,
    )
    assert json.loads(path.read_text())["excluded_from_replication_metrics"] is False


# ---------------------------------------------------------------------------
# Watchdog-diagnostics run-scoped index (additive, never a migration)
# ---------------------------------------------------------------------------
def test_append_watchdog_event_is_additive(tmp_path):
    rp.append_watchdog_event(tmp_path, "run_Z", {"reason": "heartbeat_stale", "child_pid": 1})
    rp.append_watchdog_event(tmp_path, "run_Z", {"reason": "heartbeat_stale", "child_pid": 2})
    path = rp.watchdog_events_path(tmp_path, "run_Z")
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["child_pid"] == 1
    assert json.loads(lines[1])["child_pid"] == 2
