# OPERATIONS RUNBOOK

_DRY-RUN ONLY. Nothing here enables live trading._

## 0. Golden rules

- Never run with `--live` unless `LIVE_TRADING_ACK=true` is a deliberate,
  reviewed decision. The default is safe (`DRY_RUN=true`).
- Never commit `logs/`, `.env`, secrets, caches, or generated datasets.
- Do not modify Kelly / Markov / thresholds / sizing / signal / selection logic
  without a separate, reviewed change.

## 1. Preflight (always run first)

```bash
cd ~/polymarket-BTC-15-Minute-Trading-Bot
.venv/bin/python scripts/preflight_dryrun.py
```

This verifies (without exposing secrets): dry-run flags, no `--live`, required
directories/data files, writable `logs/`, network/DNS reachability, service
state, and diagnostic signal readiness. Exit code 0 = safe to proceed.

## 2. Start a DRY-RUN shadow run (foreground, watchdog-wrapped)

```bash
cd ~/polymarket-BTC-15-Minute-Trading-Bot
.venv/bin/python 15m_bot_runner.py
```

- No `--live` flag → shadow/paper mode only.
- The runner wraps `bot.py` with an auto-restart watchdog
  (`STALE_THRESHOLD_SECONDS`); on a stall it captures diagnostics (SIGUSR1 +
  SIGUSR2) **before** restarting the child.

## 3. Stop the run

```bash
# Ctrl-C in the foreground terminal (graceful SIGINT), OR from another shell:
pkill -INT -f 15m_bot_runner.py
```

The runner installs SIGTERM/SIGINT/SIGHUP handlers and terminates the child
gracefully, logging a `child_exit` runner event.

## 4. On-demand diagnostics (manual)

```bash
# Native all-thread C traceback (works even if the interpreter is wedged):
kill -USR1 <bot_pid>      # appends to logs/diagnostics/faulthandler.log
# Rich redacted JSON snapshot (threads, asyncio tasks, recent op timings):
kill -USR2 <bot_pid>      # writes logs/diagnostics/stall_diag_*.json
```

Prove the capture path deterministically (NO network):

```bash
.venv/bin/python scripts/diag_stall_smoke_test.py   # expect RESULT: PASS
```

## 5. Post-run report (scoped)

```bash
.venv/bin/python scripts/post_run_report.py \
  --since 2026-06-26T00:43:30Z --until 2026-07-10T09:58:00Z
```

Writes a scoped Markdown/JSON report to `logs/` (directional accuracy +
corrected synthetic economics; recorded economic P/L is reported as
**unavailable** for historical records).

## 6. Validate official resolutions (offline)

```bash
.venv/bin/python scripts/validate_official_resolutions.py
```

Re-checks the on-disk store invariants over a seeded sample (≥50) plus every
top-30 P/L market; writes `logs/official_resolution_audit.md` + fixture.

## 6b. Dual-book NO-ask connectivity smoke test (read-only, bounded)

```bash
# proves a REAL, contemporaneous NO-token ask is obtainable; ≤600s hard cap.
.venv/bin/python scripts/no_book_smoke_test.py --duration 180 --interval 30 --json
```

Read-only HTTP GET only. Never starts the runner, `--live`, or any order path.
Expect `"result": "PASS"` with `captured_executable_no_ask` set and all
`validation_flags` true. Offline: add `--fixtures f.json --once`.

## 6c. Rebuild the official-resolution store safely (read-only fetch)

```bash
# report first (never writes the active store):
.venv/bin/python scripts/rebuild_official_resolutions.py --all --report-only --json
# apply (replaces active store ONLY if validation fully succeeds; backup kept):
.venv/bin/python scripts/rebuild_official_resolutions.py --all --json
```

Backs up the store to `official_resolutions.jsonl.backup-<ts>`, re-fetches every
condition id with the hardened parser, and replaces the active store only when
no mismatch/regression is found. A network failure leaves the store intact.

## 6d. Full-runner dual-book audit (read-only)

```bash
# audit every would-trade record in an explicit run window (from runner_events.jsonl)
.venv/bin/python scripts/dual_book_run_audit.py \
  --since 2026-07-19T21:16:00Z --until 2026-07-20T01:16:11Z
```

Proves per record that SHORT priced off the NO ask / LONG off the YES ask, quote
freshness/skew/spread, book generation, market identity and paper-fill L1 depth.
Writes `logs/dual_book_run_forensic.csv` + `logs/dual_book_run_audit.json`.
Official outcomes are fetched read-only; deeper liquidity is never fabricated.

## 7. Tests

```bash
.venv/bin/python -m pytest tests/ -q     # expect 325 passed (2026-07-26 closeout)
```

## 7c. DRY-RUN skips execution-state reconciliation (fixed 2026-07-26)

Two watchdog restarts in the 2026-07-20..27 collection run were traced to
Nautilus's `SystemKernel.start_async()` aborting startup (never calling
`self._trader.start()`, so no subscriptions/heartbeat ever happen) after a
transient `httpcore.RemoteProtocolError("Server disconnected")` during
startup execution-state reconciliation — the process then sat inert for
~45 minutes until the watchdog's heartbeat-staleness check killed it.
`bot.py` now passes `reconciliation=not simulation` to `LiveExecEngineConfig`:
reconciling real exchange order/fill state is meaningless while
`DRY_RUN=true` (no real order is ever placed), so the vulnerable path is
skipped entirely in dry-run. A live run (`simulation=False`, not approved)
keeps the safe `reconciliation=True` default unchanged. The watchdog is
unchanged and remains the safety net for any other stall.
See `tests/test_reconciliation_dry_run_gate.py`.

## 7b. Watchdog startup false-positive (fixed 2026-07-19)

The heartbeat staleness check is now scoped to the current run session
(`run_start_ts`), matching the journal/shadow checks. A leftover heartbeat from
a previous run can no longer trigger a spurious `heartbeat_stale` restart during
startup. A genuinely wedged startup that never writes a fresh heartbeat within a
full stale window is still restarted, with the distinct reason
`heartbeat_never_fresh`. This did **not** change the 45-min stale threshold.

Post-run / forensic commands (read-only):

```bash
# explicit-window three-view report
.venv/bin/python scripts/post_run_report.py \
    --since <ISO8601> --until <ISO8601>
# per-trade forensic CSV (0 anomaly flags expected)
.venv/bin/python scripts/forensic_v2_run.py \
    --since <ISO8601> --until <ISO8601> --out logs/forensic_v2_run.csv
# diagnostics smoke test (must print RESULT: PASS)
.venv/bin/python scripts/diag_stall_smoke_test.py
```

## 8. Hermes monitor (handoff)

See `HERMES_HANDOFF.md` for the exact monitor commands.
