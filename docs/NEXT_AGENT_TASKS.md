# NEXT AGENT TASKS

_DRY-RUN research handoff. Live trading is **NOT APPROVED**. Read this first._

## 1. Verified architecture

- BTC 15-minute Up/Down shadow/paper harness on Polymarket, on `nautilus_trader`.
- **Dual-book capture** (`core/market_data/dual_book.py::DualBookStore`, keyed by
  token id): the runner subscribes to **both** the YES/Up and NO/Down outcome
  order books, routes quote ticks by token id, and invalidates books on
  reconnect/market-switch via a subscription generation.
- **Executable pricing** (`core/strategy_brain/shadow_evaluator._forward_schema_fields`):
  LONG pays the **YES ask**, SHORT pays the **NO ask** — never the YES midpoint,
  never `1 - yes_mid`, never a stale/closed quote. Missing/stale/crossed/invalid
  → `economic_pnl_unavailable`.
- **Paper execution** (`core/strategy_brain/paper_execution.py::simulate_fill`):
  walks the observed ask ladder of the selected token for a fixed-risk stake and
  records VWAP, partial/unavailable status, and pre-fee/fee labelling. Never
  fabricates liquidity.
- **Official resolution** (`core/strategy_brain/official_resolution.py`, parser
  `clob-resolution-2`): condition-id match required, single winner, Up/Down label.
- Safety: `DRY_RUN=true`, `LIVE_TRADING_ACK=false` (env-only), watchdog + runtime
  diagnostics, hardened preflight.

## 2. Exact current HEAD

- Branch `main`, HEAD `73c0f61` or its child doc-stamp (the exact final HEAD is
  recorded in `polymarket-final-manifest.txt`). Pre-task HEAD was `8e47b76`.
  Commits are **not pushed**.

## 3. Valid vs invalid historical conclusions

- **Valid:** directional mapping (Up/Down); official resolution matching; absence
  of look-ahead; LONG executable economics (YES is the selected token).
- **Invalid / not recoverable:** all historical *recorded* economic P/L that used
  `entry_price` (a YES midpoint) as the selected-token cost — this fabricated
  SHORT payouts. Historical executable SHORT economics are **not recoverable**
  (no NO quote/timestamp was stored) and must stay reported as *unavailable*.

## 4. Result of the new full-run dual-book audit (2026-07-19/20)

- Window: `2026-07-19T21:16:00Z → 2026-07-20T01:16:10Z` (runner_start pid 17409,
  `bot_args=[]`), 4.00h, 3 children (2 planned ~90-min restarts), **0 watchdog
  restarts**.
- 240 shadow records over 16 markets; **both books `ok` for 100%** of records;
  NO/YES quote ages median ~0.23s (max 2.5s, all < 60s); cross-book skew max
  0.107s; **0 stale / 0 skew / 0 spread / 0 crossed** warnings.
- 16 would-trade decisions, **all LONG** (the window trended up); every LONG used
  the YES ask (16/16). No SHORT would-trades occurred, but **all 98 short-side
  evaluations priced off the NO ask** (0 off the YES midpoint) — the SHORT path is
  proven wired.
- Settlement: all 4 would-trade markets officially resolved (3 UP, 1 DOWN),
  hardened parser, cid match, single winner. Directional wins 3/16 (the single
  high-conviction cycle that all configs took resolved DOWN).
- Executable economics (view B, $1 fixed risk, pre-fee): net **negative and tiny
  n** — entry prices were 0.90–0.996 so wins pay pennies while a loss costs the
  full stake. **No edge; descriptive only.**
- Forensic CSV: `logs/dual_book_run_forensic.csv`; summary
  `logs/dual_book_run_audit.json`.

## 5. Remaining L1 depth limitation

- The live Nautilus quote feed is **top-of-book (L1) only**: `depth_level_count`
  was 1 for every fill. Multi-level VWAP is supported and unit-tested but was
  **not observed live**. Fills use only the observed top level; deeper liquidity
  is never fabricated. A stake exceeding top-level size yields a labelled partial
  fill (0 occurred this run).

## 6. Fee limitation

- No fee rate is exposed by the current feed, so every paper fill is labelled
  **`pre_fee`** (`fee_rate=None`). All economic numbers are **pre-fee**; real net
  P/L will be lower. Do not present pre-fee numbers as net-of-fee.

## 7. Recommended 5–7 day dry-run

```bash
cd /home/star/polymarket-BTC-15-Minute-Trading-Bot
.venv/bin/python scripts/preflight_dryrun.py          # expect RESULT: OK
DRY_RUN=true LIVE_TRADING_ACK=false \
  nohup .venv/bin/python 15m_bot_runner.py > logs/dryrun_$(date -u +%Y%m%dT%H%M%SZ).log 2>&1 &
# let it run 5–7 days (self-restarts clean children ~every 90 min)
```
Stop: `pkill -TERM -f 15m_bot_runner.py`.
Audit afterwards with the exact window from `logs/runner_events.jsonl`:
```bash
.venv/bin/python scripts/dual_book_run_audit.py --since <ISO> --until <ISO>
```

## 8. Minimum evidence before ANY strategy change

- Sufficient **resolved** observations **per variant** (target ≥ 100 per side,
  not just per run) with executable selected-token quotes.
- Quote completeness (both books `ok`, fresh, low skew) ≥ 95% of decisions.
- Official outcome coverage 100% of would-trade markets.
- Economics reported **post-fee** (or explicitly pre-fee) with Wilson CIs and
  **out-of-sample** confirmation (separate weeks).
- A directional/economic edge whose CI excludes break-even after fees.

## 9. Explicit prohibition on live trading

- **Live trading is NOT APPROVED.** Never run with `--live`; never set
  `LIVE_TRADING_ACK=true`; never enable/start `polymarket-dryrun.service` for
  live mode; never touch order-submission code. No economic edge has been
  established.

## 10. Exact Hermes read-only monitoring commands

```bash
# health snapshot (read-only)
hermes -z "In ~/polymarket-BTC-15-Minute-Trading-Bot report: (1) is \
15m_bot_runner.py running, (2) age of logs/bot_heartbeat.json, (3) any \
watchdog_restart in logs/runner_events.jsonl in the last 24h + reason, (4) count \
of new logs/diagnostics/stall_diag_*.json today. Report only, change nothing." -t files

# dual-book capability check (read-only GET, bounded, never starts the runner)
.venv/bin/python scripts/no_book_smoke_test.py --duration 180 --interval 30 --json

# post-run dual-book audit for the latest window (read-only)
.venv/bin/python scripts/dual_book_run_audit.py --since <ISO> --until <ISO>
```
Hermes must remain **monitoring-only** (`-t files`): never edit strategy, never
start a run, never enable live trading. See `HERMES_HANDOFF.md`.
