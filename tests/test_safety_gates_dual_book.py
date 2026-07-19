"""
Safety-gate tests for the new dual-book / paper-execution diagnostic code.

These guard the DRY-RUN contract: the new modules never place, sign, submit or
simulate submission of a real order; the CLOB/Gamma client is read-only (HTTP
GET only); and the live-trading acknowledgement defaults to False and can only
come from the environment.
"""
import os
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

NEW_SOURCES = [
    project_root / "core" / "market_data" / "dual_book.py",
    project_root / "core" / "market_data" / "clob_client.py",
    project_root / "core" / "market_data" / "__init__.py",
    project_root / "core" / "strategy_brain" / "paper_execution.py",
    project_root / "scripts" / "no_book_smoke_test.py",
    project_root / "scripts" / "rebuild_official_resolutions.py",
]

# Real order-submission surface that must never appear in diagnostic code.
BANNED = [
    "submit_order", "place_order", "_place_real_order", "create_order",
    "post_order", "ClobClient(", "OrderArgs", ".sign(", "private_key",
]


@pytest.mark.parametrize("path", NEW_SOURCES, ids=lambda p: p.name)
def test_no_live_order_surface_in_new_modules(path):
    src = path.read_text()
    for token in BANNED:
        assert token not in src, f"{path.name} must not reference {token!r}"


def test_clob_client_is_read_only_http_get():
    src = (project_root / "core" / "market_data" / "clob_client.py").read_text()
    # Only GET-style urllib requests; no POST/PUT/DELETE data payloads.
    for verb in ("POST", "PUT", "DELETE", "method=\"POST\"", "data="):
        assert verb not in src, f"clob_client must not use {verb!r}"
    assert "urlopen" in src or "url_open" in src or "Request(" in src


def test_live_trading_ack_defaults_false(monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ACK", raising=False)
    import importlib
    import config as cfg
    importlib.reload(cfg)
    conf = cfg.load_strategy_config()
    assert conf["live_trading_ack"] is False
    assert conf.get("dry_run", True) is True


def test_live_trading_ack_only_from_env(monkeypatch):
    # Even if YAML/overrides tried to set it, it comes only from the env.
    monkeypatch.setenv("LIVE_TRADING_ACK", "true")
    import importlib
    import config as cfg
    importlib.reload(cfg)
    conf = cfg.load_strategy_config(overrides={"live_trading_ack": False})
    assert conf["live_trading_ack"] is True
    monkeypatch.delenv("LIVE_TRADING_ACK", raising=False)
    importlib.reload(cfg)
