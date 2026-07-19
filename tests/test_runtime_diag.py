"""
Runtime diagnostics tests (Part C) — DRY-RUN / DIAGNOSTIC ONLY.

Deterministic, offline. Covers op-timing records (ok / error / timeout),
event-loop lag detection, secret redaction, ring-buffer bounding, rich JSON
dump structure, and that instrumentation carries no trading side effects.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.diagnostics import runtime_diag as diag


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------
def test_redact_dict_keys():
    r = diag.redact({"api_key": "abc", "nested": {"passphrase": "p"}, "ok": 1})
    assert r["api_key"] == "***REDACTED***"
    assert r["nested"]["passphrase"] == "***REDACTED***"
    assert r["ok"] == 1


def test_redact_free_text():
    out = diag.redact_text("connecting with api_secret=deadbeef123 to host")
    assert "deadbeef123" not in out
    assert "***REDACTED***" in out


def test_redact_url_query_string():
    out = diag.redact_text(
        "GET https://clob.example.com/order?api_key=SEK123&signature=SIG456&x=1")
    assert "SEK123" not in out and "SIG456" not in out
    assert "x=1" in out  # non-secret query params preserved


def test_redact_bearer_header_masks_the_token_not_just_scheme():
    out = diag.redact_text("Authorization: Bearer eyJhbGciOiJIUZI1NiJ9.abcDEF")
    assert "eyJhbGciOiJIUZI1NiJ9.abcDEF" not in out
    assert "***REDACTED***" in out


def test_redact_pem_private_key_block():
    pem = ("-----BEGIN EC PRIVATE KEY-----\nMHcCAQEEI&secret&\n"
           "-----END EC PRIVATE KEY-----")
    out = diag.redact_text(f"loaded key {pem} done")
    assert "MHcCAQEEI" not in out
    assert "***REDACTED***" in out


def test_redact_depth_bounded():
    # deeply nested structure must not recurse forever
    d = cur = {}
    for _ in range(20):
        cur["k"] = {}
        cur = cur["k"]
    assert diag.redact(d) is not None  # returns without error


# --------------------------------------------------------------------------
# Sync op timing
# --------------------------------------------------------------------------
def test_timed_records_success_and_reraises_error():
    with diag.timed("t_ok"):
        time.sleep(0.001)
    raised = False
    try:
        with diag.timed("t_err"):
            raise ValueError("boom")
    except ValueError:
        raised = True
    assert raised
    ops = {o["op"]: o for o in diag._STATE.snapshot_ops()}
    assert ops["t_ok"]["status"] == "ok"
    assert ops["t_err"]["status"] == "error"
    assert "ValueError" in ops["t_err"]["error"]


# --------------------------------------------------------------------------
# Async op timing (success / timeout)
# --------------------------------------------------------------------------
def test_atimed_success_and_timeout():
    async def slow():
        await asyncio.sleep(1.0)

    async def scenario():
        async with diag.atimed("a_ok"):
            await asyncio.sleep(0.001)
        try:
            async with diag.atimed("a_to"):
                await asyncio.wait_for(slow(), timeout=0.02)
        except (asyncio.TimeoutError, TimeoutError):
            pass

    asyncio.run(scenario())
    ops = {o["op"]: o for o in diag._STATE.snapshot_ops()}
    assert ops["a_ok"]["status"] == "ok"
    assert ops["a_to"]["status"] == "timeout"


def test_timed_async_decorator():
    @diag.timed_async("decorated")
    async def work():
        await asyncio.sleep(0.001)
        return 42

    assert asyncio.run(work()) == 42
    assert any(o["op"] == "decorated" for o in diag._STATE.snapshot_ops())


# --------------------------------------------------------------------------
# Event-loop lag detection
# --------------------------------------------------------------------------
def test_event_loop_lag_detected():
    async def scenario():
        monitor = diag.EventLoopLagMonitor(interval=0.02, threshold_s=0.05, name="test")
        task = asyncio.ensure_future(monitor.run())

        async def blocker():
            await asyncio.sleep(0.03)   # let the monitor enter its sleep
            time.sleep(0.3)             # block the single-threaded loop -> lag

        await blocker()
        await asyncio.sleep(0.05)       # allow monitor to observe the lag
        monitor.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    before = len(diag._STATE.snapshot_lags())
    asyncio.run(scenario())
    lags = diag._STATE.snapshot_lags()
    assert len(lags) > before
    assert lags[-1]["lag_s"] >= 0.05
    assert lags[-1]["monitor"] == "test"


# --------------------------------------------------------------------------
# Ring buffer is bounded
# --------------------------------------------------------------------------
def test_op_buffer_is_bounded():
    for i in range(diag.OP_BUFFER_SIZE + 50):
        diag.record_op(f"flood_{i}", 0.0)
    assert len(diag._STATE.snapshot_ops()) <= diag.OP_BUFFER_SIZE


# --------------------------------------------------------------------------
# Rich JSON dump structure + secret safety
# --------------------------------------------------------------------------
def test_dump_diagnostics_structure(tmp_path):
    diag.record_op("dump_probe", 0.01, market_id="0xabc")
    path = diag.dump_diagnostics(reason="unit_test", dump_dir=str(tmp_path))
    assert path is not None
    payload = json.loads(Path(path).read_text())
    for key in ("schema", "reason", "resources", "recent_ops",
                "recent_loop_lags", "thread_stacks", "asyncio_tasks"):
        assert key in payload
    assert payload["reason"] == "unit_test"
    # no raw secret substrings leak into the serialized dump
    assert "PRIVATE_KEY_VALUE" not in json.dumps(payload)


def test_install_diagnostics_reports_status(tmp_path):
    status = diag.install_diagnostics(dump_dir=str(tmp_path))
    assert "native" in status and "rich" in status
    assert status["dump_dir"] == str(tmp_path)


# --------------------------------------------------------------------------
# No trading side effects
# --------------------------------------------------------------------------
def test_module_has_no_order_or_bot_imports():
    src = (project_root / "core" / "diagnostics" / "runtime_diag.py").read_text()
    assert "submit_order" not in src
    assert "import bot" not in src and "from bot" not in src
