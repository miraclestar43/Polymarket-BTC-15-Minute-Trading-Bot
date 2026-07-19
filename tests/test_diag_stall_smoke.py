"""
Synthetic-stall smoke test (Part C) — DIAGNOSTIC ONLY, NO NETWORK.

Runs the deterministic fork-based smoke test from
`scripts/diag_stall_smoke_test.py` and asserts the watchdog-style capture path
produces a native faulthandler traceback AND a redacted rich JSON dump. Skipped
where `os.fork` is unavailable (e.g. Windows).
"""
import os
import sys
import json
import tempfile
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "scripts"))

pytestmark = pytest.mark.skipif(
    not hasattr(os, "fork"), reason="requires os.fork")


def test_synthetic_stall_capture(tmp_path):
    import diag_stall_smoke_test as smoke

    res = smoke.run_smoke_test(str(tmp_path / "diagnostics"))

    assert res["faulthandler_grew"] is True, "native SIGUSR1 dump not captured"
    assert res["rich_dump"] is not None, "rich SIGUSR2 JSON dump not captured"

    payload = json.loads(Path(res["rich_dump"]).read_text())
    assert payload["schema"] == "runtime-diag-1.0"
    # The synthetic op history is present and secrets are redacted.
    blob = json.dumps(payload)
    assert "SHOULD_BE_REDACTED" not in blob
    ops = {o["op"] for o in payload["recent_ops"]}
    assert "synthetic_wedge" in ops
