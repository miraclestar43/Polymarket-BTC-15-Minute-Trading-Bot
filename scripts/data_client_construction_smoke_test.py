#!/usr/bin/env python3
"""
Data-client construction smoke test — DRY-RUN / DIAGNOSTIC ONLY.

Reproduces the EXACT construction path that crashed on 2026-08-01
(run_1785613687_96724, polymarket-collection-7d-w3.service): builds a
TradingNode with only a Polymarket data-client configured and calls
node.build(), which is precisely the call chain that reached
PolymarketDataClient.__init__ and previously raised

    AttributeError: attribute '_log' of 'nautilus_trader.common.component.Component'
    objects is not writable

Bounded and fully offline:
  - constructs objects only, never calls node.run() or connects any client;
  - never touches market-data or trading networks;
  - uses hardcoded placeholder credential strings, never .env or real secrets;
  - intended to be run with an external timeout (see main() in this file,
    or invoke via `timeout 30s .venv/bin/python scripts/...`).

Exit 0 + "RESULT: PASS" means construction reached past the previous
failure point and the data client's `_log` is still the original,
untouched Nautilus Logger instance (proving the current patch, if any,
never assigns Component._log). Any other outcome prints "RESULT: FAIL"
with the exception.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Apply the exact same patches bot.py applies, in the same order, so this
# test exercises the real, currently-installed patch chain.
from patch_gamma_markets import apply_gamma_markets_patch  # noqa: E402
from patch_market_orders import apply_market_order_patch  # noqa: E402
from patch_quote_tick_logging import apply_quote_tick_logging_patch  # noqa: E402

# Placeholder-only: never read from .env, never a real credential. Passing
# None for api_key/api_secret/passphrase would make nautilus fall back to
# reading POLYMARKET_API_KEY/etc. from the environment (verified: raises
# RuntimeError if unset) -- explicit dummy strings avoid that fallback
# entirely, so this script never touches real or environment credentials.
_DUMMY_PRIVATE_KEY = "0x" + "1" * 64
_DUMMY_FUNDER = "0x" + "2" * 40
_DUMMY_API_KEY = "smoke-test-dummy-api-key"
_DUMMY_API_SECRET = "smoke-test-dummy-api-secret"
_DUMMY_PASSPHRASE = "smoke-test-dummy-passphrase"


def _build_minimal_node():
    from nautilus_trader.adapters.polymarket import POLYMARKET
    from nautilus_trader.adapters.polymarket import PolymarketDataClientConfig
    from nautilus_trader.adapters.polymarket.factories import PolymarketLiveDataClientFactory
    from nautilus_trader.config import (
        InstrumentProviderConfig,
        LiveDataEngineConfig,
        LiveRiskEngineConfig,
        LoggingConfig,
        TradingNodeConfig,
    )
    from nautilus_trader.live.node import TradingNode

    instrument_cfg = InstrumentProviderConfig(load_all=False, use_gamma_markets=True)
    poly_data_cfg = PolymarketDataClientConfig(
        private_key=_DUMMY_PRIVATE_KEY,
        api_key=_DUMMY_API_KEY,
        api_secret=_DUMMY_API_SECRET,
        passphrase=_DUMMY_PASSPHRASE,
        funder=_DUMMY_FUNDER,
        signature_type=1,
        instrument_provider=instrument_cfg,
    )

    config = TradingNodeConfig(
        environment="live",
        trader_id="QUOTE-TICK-SMOKE-TEST-001",
        # bypass_logging=True is rejected by nautilus in a LIVE-environment
        # kernel (system/kernel.py raises InvalidConfiguration) -- verified
        # empirically, not assumed. log_level="ERROR" keeps output minimal
        # without needing to bypass logging.
        logging=LoggingConfig(log_level="ERROR", log_directory=None),
        data_engine=LiveDataEngineConfig(qsize=100),
        # No exec_engine/exec_clients configured -- this smoke test only
        # needs to reach PolymarketDataClient construction; omitting the
        # exec client keeps the test to the narrowest reproduction of the
        # actual failure (node.build() -> build_data_clients()).
        risk_engine=LiveRiskEngineConfig(bypass=True),
        data_clients={POLYMARKET: poly_data_cfg},
    )

    node = TradingNode(config=config)
    node.add_data_client_factory(POLYMARKET, PolymarketLiveDataClientFactory)
    return node


def run() -> int:
    print("=== data-client construction smoke test ===")
    print("Applying patches (same order as bot.py)...")
    ok1 = apply_gamma_markets_patch()
    ok2 = apply_market_order_patch()
    ok3 = apply_quote_tick_logging_patch()
    print(f"  gamma_markets patch applied:   {ok1}")
    print(f"  market_order patch applied:    {ok2}")
    print(f"  quote_tick_logging patch:      {ok3}")

    try:
        node = _build_minimal_node()
        print("Calling node.build() -- the exact call chain that previously crashed...")
        node.build()  # <-- this is the exact line that raised AttributeError before
    except Exception as e:  # noqa: BLE001 -- report any failure, do not hide it
        print(f"RESULT: FAIL -- {type(e).__name__}: {e}")
        return 1

    # Reach into the constructed client and prove `_log` is still the
    # genuine, original Nautilus Logger -- never swapped, never proxied.
    # DataEngine has no public get_client() method; `_clients` is declared
    # `cdef readonly dict[ClientId, DataClient]` in engine.pxd, so it is
    # readable (not writable) from Python -- verified against the real
    # engine.pxd/.pyx source, not assumed.
    try:
        from nautilus_trader.common.component import Logger

        data_engine = node.kernel.data_engine
        client_ids = data_engine.registered_clients
        client_id = client_ids[0] if client_ids else None
        client = data_engine._clients.get(client_id) if client_id else None
        if client is None:
            print("RESULT: FAIL -- could not locate the constructed data client")
            return 1
        is_real_logger = isinstance(client._log, Logger)
        is_our_proxy = type(client._log).__module__ == "patch_quote_tick_logging"
        print(f"  client._log is genuine nautilus Logger: {is_real_logger}")
        print(f"  client._log is a patch_quote_tick_logging proxy: {is_our_proxy}")
        if not is_real_logger or is_our_proxy:
            print("RESULT: FAIL -- _log is not the original, untouched Logger instance")
            return 1
    except Exception as e:  # noqa: BLE001
        print(f"RESULT: FAIL -- post-build verification error: {type(e).__name__}: {e}")
        return 1

    print("RESULT: PASS -- construction reached past the previous failure point; "
          "_log is the original, untouched Nautilus Logger object; "
          "node.run() was never called; no network connection was made.")
    return 0


def main() -> int:
    # No network client's connect()/run() is ever invoked in run(), so this
    # completes in well under a second in practice; an external `timeout`
    # wrapper is still recommended when invoking this script (see module
    # docstring) as defense in depth.
    return run()


if __name__ == "__main__":
    sys.exit(main())
