"""
Preflight replication-safety checks — DRY-RUN / DIAGNOSTIC, deterministic,
offline. Covers the checks added for Week-2 replication readiness: Git
HEAD/dirty-state reporting, --require-clean-git escalation, config hashing,
and the existing dry-run safety gates (unchanged).
"""
import subprocess
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

import preflight_dryrun as pf  # noqa: E402


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
# _check_git_state
# ---------------------------------------------------------------------------
def test_git_state_pass_on_clean_repo(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(pf, "PROJECT_ROOT", repo)
    chk = pf.Check()
    pf._check_git_state(chk, require_clean=False)
    rows = dict((n, (s, d)) for n, s, d in chk.rows)
    assert rows["git HEAD"][0] == pf.PASS
    assert rows["working tree clean"][0] == pf.PASS
    assert not chk.blocking_fail


def test_git_state_warn_on_dirty_without_flag(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    (repo / "f.txt").write_text("uncommitted change\n")
    monkeypatch.setattr(pf, "PROJECT_ROOT", repo)
    chk = pf.Check()
    pf._check_git_state(chk, require_clean=False)
    rows = dict((n, (s, d)) for n, s, d in chk.rows)
    assert rows["working tree clean"][0] == pf.WARN
    assert not chk.blocking_fail  # WARN alone must not block


def test_require_clean_git_escalates_dirty_tree_to_blocking_fail(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    (repo / "f.txt").write_text("uncommitted change\n")
    monkeypatch.setattr(pf, "PROJECT_ROOT", repo)
    chk = pf.Check()
    pf._check_git_state(chk, require_clean=True)
    rows = dict((n, (s, d)) for n, s, d in chk.rows)
    assert rows["working tree clean"][0] == pf.FAIL
    assert chk.blocking_fail


def test_require_clean_git_still_passes_on_clean_tree(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(pf, "PROJECT_ROOT", repo)
    chk = pf.Check()
    pf._check_git_state(chk, require_clean=True)
    rows = dict((n, (s, d)) for n, s, d in chk.rows)
    assert rows["working tree clean"][0] == pf.PASS
    assert not chk.blocking_fail


def test_git_state_unavailable_never_blocks(tmp_path, monkeypatch):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    monkeypatch.setattr(pf, "PROJECT_ROOT", not_a_repo)
    chk = pf.Check()
    pf._check_git_state(chk, require_clean=True)
    rows = dict((n, (s, d)) for n, s, d in chk.rows)
    assert rows["git HEAD"][0] == pf.WARN
    assert rows["working tree clean"][0] == pf.WARN
    assert not chk.blocking_fail  # unavailable is a WARN, never a silent FAIL


# ---------------------------------------------------------------------------
# _check_config_hashes
# ---------------------------------------------------------------------------
def test_config_hashes_present_in_real_repo():
    chk = pf.Check()
    pf._check_config_hashes(chk)
    names = [r[0] for r in chk.rows]
    assert "strategy.yaml hash" in names
    assert "requirements.txt hash" in names
    assert not chk.blocking_fail


def test_config_hashes_warn_when_files_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "PROJECT_ROOT", tmp_path)
    chk = pf.Check()
    pf._check_config_hashes(chk)
    rows = dict((n, (s, d)) for n, s, d in chk.rows)
    assert rows["strategy.yaml hash"][0] == pf.WARN
    assert not chk.blocking_fail


# ---------------------------------------------------------------------------
# --require-clean-git CLI wiring (main())
# ---------------------------------------------------------------------------
def test_cli_flag_registered():
    ap_actions = pf.argparse.ArgumentParser()
    # smoke-test that main() parses the flag without error via the real parser
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = pf.main(["--require-clean-git"])
    assert code in (0, 1)  # must not raise; real repo state decides pass/block
    assert "RESULT:" in buf.getvalue()


# ---------------------------------------------------------------------------
# Unchanged dry-run safety gates
# ---------------------------------------------------------------------------
def test_no_live_flag_check_unchanged():
    chk = pf.Check()
    pf._check_no_live_flag(chk, [])
    assert chk.rows[0][1] == pf.PASS
    chk2 = pf.Check()
    pf._check_no_live_flag(chk2, ["--live"])
    assert chk2.rows[0][1] == pf.FAIL
    assert chk2.blocking_fail


def test_dry_run_check_unchanged():
    chk = pf.Check()
    pf._check_dry_run(chk)
    rows = dict((n, (s, d)) for n, s, d in chk.rows)
    assert rows["dry_run=true"][0] == pf.PASS
    assert rows["LIVE_TRADING_ACK not true"][0] == pf.PASS
