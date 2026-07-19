"""Runtime diagnostics package (stall investigation, read-only instrumentation)."""

from .runtime_diag import (  # noqa: F401
    EventLoopLagMonitor,
    NET_TIMEOUT_S,
    atimed,
    dump_diagnostics,
    install_diagnostics,
    record_op,
    redact,
    redact_text,
    register_loop,
    timed,
    timed_async,
)
