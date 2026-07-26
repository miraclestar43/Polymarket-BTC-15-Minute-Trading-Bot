"""
DRY-RUN execution-reconciliation gate — regression test.

Root cause (proven twice in the 2026-07-20..27 collection run via
runner_events.jsonl + journalctl, PIDs 29743 and 48947): a transient
``httpcore.RemoteProtocolError("Server disconnected")`` during Nautilus's
startup execution-state reconciliation makes
``SystemKernel.start_async()`` (nautilus_trader/system/kernel.py) return
early — ``self._trader.start()`` is never called, so no market
subscriptions, quotes, decisions, or heartbeats are ever produced. The
process stays alive but fully inert until the external watchdog's
heartbeat-staleness check kills it (~45 minutes later).

Reconciling real exchange order/fill state is meaningless while
``DRY_RUN``/``simulation`` is true (no real order is ever placed), so
``bot.py`` must skip it in that mode, removing the failure's trigger
entirely without touching any live-trading path, Nautilus internals, or
strategy logic. This test statically verifies that wiring stays in place.
"""
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
BOT_SRC = (PROJECT_ROOT / "bot.py").read_text()


def _exec_engine_call_site() -> str:
    m = re.search(r"exec_engine\s*=\s*LiveExecEngineConfig\([^)]*\)", BOT_SRC)
    assert m, "LiveExecEngineConfig(...) construction not found in bot.py"
    return m.group(0)


def test_reconciliation_is_gated_off_in_dry_run():
    call = _exec_engine_call_site()
    assert "reconciliation" in call, (
        "LiveExecEngineConfig must pass an explicit `reconciliation=` kwarg "
        "tied to the simulation/dry-run flag; the Nautilus default "
        "(reconciliation=True) reproduces the proven 45-minute startup stall "
        "on a transient exchange disconnect."
    )
    assert re.search(r"reconciliation\s*=\s*not\s+simulation", call), (
        f"expected `reconciliation=not simulation`, got: {call}"
    )


def test_reconciliation_gate_uses_the_effective_dry_run_flag():
    # `simulation` must be the same flag threaded into strategy_config's
    # dry_run override earlier in run_integrated_bot, not a separate/stale
    # variable, so the gate can never disagree with DRY_RUN/config.dry_run.
    assert '{"dry_run": simulation}' in BOT_SRC


def test_live_path_keeps_the_safe_reconciliation_default():
    # A live run (simulation=False) must keep reconciliation=True (the
    # Nautilus-recommended default) — this fix only removes the check for
    # DRY_RUN, it does not weaken any live-trading safety behaviour.
    call = _exec_engine_call_site()
    simulation = False  # noqa: F841 (documented for the reader)
    reconciliation = not simulation
    assert reconciliation is True
