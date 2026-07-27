"""
Isolation preflight (--require-isolated-run) — DRY-RUN / DIAGNOSTIC,
deterministic, offline. Proves the exact failure mode found in the first
Week-2 pilot (run_1785102520_55073, which ran ~6h alongside the still-active
Week-1 runner) is now a hard, detectable, blockable preflight condition.
"""
import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

import preflight_dryrun as pf  # noqa: E402


# ---------------------------------------------------------------------------
# Low-level process matching: exact-basename + cwd check, no subprocess self-match
# ---------------------------------------------------------------------------
def test_find_project_processes_matches_exact_basename_and_cwd(monkeypatch):
    monkeypatch.setattr(pf, "PROJECT_ROOT", Path("/fake/project"))
    fake_procs = [
        (111, ["/usr/bin/python3", "/fake/project/15m_bot_runner.py"]),
        (222, ["/usr/bin/python3", "/other/project/15m_bot_runner.py"]),  # wrong cwd
        (333, ["some-editor", "/fake/project/15m_bot_runner.py.bak"]),     # not exact basename
    ]
    monkeypatch.setattr(pf, "_iter_proc_cmdlines", lambda: iter(fake_procs))
    monkeypatch.setattr(pf, "_proc_cwd", lambda pid: "/fake/project" if pid == 111 else "/other/project")
    matches = pf._find_project_processes("15m_bot_runner.py", exclude_pid=999)
    assert matches == [111]


def test_find_project_processes_excludes_own_pid(monkeypatch):
    monkeypatch.setattr(pf, "PROJECT_ROOT", Path("/fake/project"))
    monkeypatch.setattr(pf, "_iter_proc_cmdlines",
                        lambda: iter([(555, ["python", "15m_bot_runner.py"])]))
    monkeypatch.setattr(pf, "_proc_cwd", lambda pid: "/fake/project")
    assert pf._find_project_processes("15m_bot_runner.py", exclude_pid=555) == []


def test_find_project_processes_no_false_positive_from_grep_or_editor(monkeypatch):
    # A grep/pgrep-style process whose OWN argv happens to contain the search
    # text (e.g. `grep 15m_bot_runner.py`), or an editor with the path open,
    # must not match unless the basename is an EXACT argv token.
    monkeypatch.setattr(pf, "PROJECT_ROOT", Path("/fake/project"))
    fake_procs = [
        (777, ["grep", "-r", "15m_bot_runner.py", "/fake/project"]),
        (888, ["code", "--file", "/fake/project/15m_bot_runner.py:42"]),
    ]
    monkeypatch.setattr(pf, "_iter_proc_cmdlines", lambda: iter(fake_procs))
    monkeypatch.setattr(pf, "_proc_cwd", lambda pid: "/fake/project")
    assert pf._find_project_processes("15m_bot_runner.py", exclude_pid=999) == []


def test_real_process_scan_does_not_self_match(tmp_path):
    # Run the REAL (non-mocked) scanner against the REAL /proc: this test's
    # own process (pytest) must never appear as a match for either script.
    import os
    my_pid = os.getpid()
    runner_matches = pf._find_project_processes("15m_bot_runner.py", exclude_pid=my_pid)
    bot_matches = pf._find_project_processes("bot.py", exclude_pid=my_pid)
    assert my_pid not in runner_matches
    assert my_pid not in bot_matches


# ---------------------------------------------------------------------------
# Unclosed run manifest detection
# ---------------------------------------------------------------------------
def test_unclosed_run_manifest_detected(tmp_path):
    run_dir = tmp_path / "logs" / "runs" / "run_ABC"
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text("{}")
    assert pf._find_unclosed_run_manifests(tmp_path) == ["run_ABC"]


def test_run_with_end_manifest_is_not_unclosed(tmp_path):
    run_dir = tmp_path / "logs" / "runs" / "run_ABC"
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text("{}")
    (run_dir / "run_manifest_end.json").write_text("{}")
    assert pf._find_unclosed_run_manifests(tmp_path) == []


def test_run_with_abort_manifest_is_not_unclosed(tmp_path):
    run_dir = tmp_path / "logs" / "runs" / "run_XYZ"
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text("{}")
    (run_dir / "abort_manifest.json").write_text("{}")
    assert pf._find_unclosed_run_manifests(tmp_path) == []


def test_no_runs_directory_is_not_an_error(tmp_path):
    assert pf._find_unclosed_run_manifests(tmp_path) == []


# ---------------------------------------------------------------------------
# _check_isolated_run: WARN by default, blocking FAIL under --require-isolated-run
# ---------------------------------------------------------------------------
def test_isolated_run_check_passes_clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pf, "_find_project_processes", lambda *a, **k: [])
    monkeypatch.setattr(pf.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": ""})())
    chk = pf.Check()
    pf._check_isolated_run(chk, require_isolated=True)
    assert not chk.blocking_fail
    rows = dict((n, s) for n, s, _ in chk.rows)
    assert rows["no competing 15m_bot_runner.py"] == pf.PASS
    assert rows["no competing bot.py child"] == pf.PASS
    assert rows["no unclosed run manifest"] == pf.PASS


def test_competing_runner_warns_without_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pf, "_find_project_processes",
                        lambda name, exclude_pid: [19538] if name == "15m_bot_runner.py" else [])
    monkeypatch.setattr(pf.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": ""})())
    chk = pf.Check()
    pf._check_isolated_run(chk, require_isolated=False)
    assert not chk.blocking_fail  # WARN alone must not block
    rows = dict((n, s) for n, s, _ in chk.rows)
    assert rows["no competing 15m_bot_runner.py"] == pf.WARN


def test_competing_runner_blocks_with_require_isolated_run(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pf, "_find_project_processes",
                        lambda name, exclude_pid: [19538] if name == "15m_bot_runner.py" else [])
    monkeypatch.setattr(pf.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": ""})())
    chk = pf.Check()
    pf._check_isolated_run(chk, require_isolated=True)
    assert chk.blocking_fail
    rows = dict((n, s) for n, s, _ in chk.rows)
    assert rows["no competing 15m_bot_runner.py"] == pf.FAIL


def test_competing_bot_child_blocks_with_require_isolated_run(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pf, "_find_project_processes",
                        lambda name, exclude_pid: [55976] if name == "bot.py" else [])
    monkeypatch.setattr(pf.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": ""})())
    chk = pf.Check()
    pf._check_isolated_run(chk, require_isolated=True)
    assert chk.blocking_fail


def test_active_collection_service_blocks_with_require_isolated_run(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pf, "_find_project_processes", lambda *a, **k: [])

    def fake_run(*a, **k):
        return type("R", (), {
            "stdout": "polymarket-collection-7d.service loaded active running Description\n"
        })()
    monkeypatch.setattr(pf.subprocess, "run", fake_run)
    chk = pf.Check()
    pf._check_isolated_run(chk, require_isolated=True)
    assert chk.blocking_fail
    rows = dict((n, s) for n, s, _ in chk.rows)
    assert rows["no active collection service"] == pf.FAIL


def test_unclosed_manifest_blocks_with_require_isolated_run(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pf, "_find_project_processes", lambda *a, **k: [])
    monkeypatch.setattr(pf.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": ""})())
    run_dir = tmp_path / "logs" / "runs" / "run_orphan"
    run_dir.mkdir(parents=True)
    (run_dir / "run_manifest.json").write_text("{}")
    chk = pf.Check()
    pf._check_isolated_run(chk, require_isolated=True)
    assert chk.blocking_fail
    rows = dict((n, s) for n, s, _ in chk.rows)
    assert rows["no unclosed run manifest"] == pf.FAIL


# ---------------------------------------------------------------------------
# --require-isolated-run wired into main()
# ---------------------------------------------------------------------------
def test_main_accepts_require_isolated_run_flag():
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = pf.main(["--require-isolated-run"])
    assert code in (0, 1)
    assert "RESULT:" in buf.getvalue()


def test_main_clean_state_passes_both_flags_combined():
    # The real repo, right now, has no competing runner/service/orphan
    # manifest and (may or may not have) a clean tree -- assert only on the
    # isolation-specific rows, not overall exit code (git dirtiness is
    # independent and covered elsewhere).
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        pf.main(["--require-isolated-run"])
    out = buf.getvalue()
    assert "[PASS] no competing 15m_bot_runner.py" in out
    assert "[PASS] no competing bot.py child" in out
    assert "[PASS] no active collection service" in out
    assert "[PASS] no unclosed run manifest" in out
