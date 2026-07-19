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

## 7. Tests

```bash
.venv/bin/python -m pytest tests/ -q     # expect 263 passed
```

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
