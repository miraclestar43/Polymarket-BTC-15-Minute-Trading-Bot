# HERMES HANDOFF

_How to hand off monitoring of the DRY-RUN bot to the Hermes agent._

Hermes CLI: `/home/star/.venvs/hermes/bin/hermes` (alias `hermes`).
This document only covers **monitoring** — Hermes must never place trades or
enable live mode.

## 1. Quick health check (ad hoc)

```bash
# Hermes components + provider status
hermes status

# Ask Hermes to summarise the latest run health from logs (read-only)
hermes -z "Check ~/polymarket-BTC-15-Minute-Trading-Bot: is 15m_bot_runner.py \
running, how old is logs/bot_heartbeat.json, and were there any \
watchdog_restart events today? Report only — do not change anything." \
  -t files
```

## 2. Scheduled monitoring (cron)

```bash
# List existing scheduled jobs
hermes cron list

# Add a periodic (e.g. every 15 min) read-only health check that alerts on stall
hermes cron add \
  --name polymarket-dryrun-health \
  --schedule "*/15 * * * *" \
  --prompt "Inspect ~/polymarket-BTC-15-Minute-Trading-Bot heartbeat and runner \
events. If bot_heartbeat.json is older than 15 minutes or a watchdog_restart \
occurred since the last check, send an alert. Read-only." \
  --toolsets files
```

## 3. Alerts / notifications

```bash
# One-off notification to a configured platform (Slack/WhatsApp/etc.)
hermes send "polymarket dry-run: heartbeat stale > 15m — check the runner"
```

## 4. What Hermes should watch

- `logs/bot_heartbeat.json` age (stall symptom).
- Runner events with `event == "watchdog_restart"` (capture `diagnostics`
  paths: `logs/diagnostics/faulthandler.log`, `logs/diagnostics/stall_diag_*.json`).
- New `logs/diagnostics/stall_diag_*.json` files (a stall was captured).
- Whether `15m_bot_runner.py` is still running.

## 5. Guardrails for the Hermes operator

- Read-only: `-t files` / `--toolsets files`. Do **not** grant shell tools that
  could start `--live` or stop safety services.
- Never ask Hermes to edit `.env`, `strategy.yaml` trading params, or run with
  `--live`.
- Escalate to a human before any action that could place an order or enable
  live trading.

## 6. Session review

```bash
hermes sessions list        # recent Hermes monitoring sessions
hermes logs                 # Hermes run logs
```

## 7. Hermes command sets (final handoff)

Hermes is a **monitoring** agent. It must **not** edit strategy code, change
trading parameters, or initiate live trading. All prompts below are read-only
(`-t files`).

### 7.1 Daily monitoring prompt

```bash
hermes -z "In ~/polymarket-BTC-15-Minute-Trading-Bot report: (1) is \
15m_bot_runner.py running, (2) age of logs/bot_heartbeat.json, (3) any \
watchdog_restart events in logs/runner_events.jsonl in the last 24h and their \
reason, (4) count of new logs/diagnostics/stall_diag_*.json today. Report only, \
change nothing." -t files
```

### 7.2 Post-run analysis prompt

```bash
hermes -z "For ~/polymarket-BTC-15-Minute-Trading-Bot, using the most recent \
runner_start..(next runner_start|now) window from logs/runner_events.jsonl, run \
(read-only) scripts/post_run_report.py and scripts/forensic_v2_run.py for that \
window and summarise: record count, would_trade count, directional accuracy per \
variant with n, view-B executable economics (LONG only), the unavailable set, \
and any forensic anomaly flags. Do not present view-C midpoint P/L as achieved. \
Do not change strategy or start a run." -t files
```

### 7.3 Schema-v2 quote-completeness commands

```bash
# Every new record must be schema_version 2; shorts must be honest.
D=logs/shadow_decisions_$(date -u +%F).jsonl
python3 -c "import json,sys; rs=[json.loads(l) for l in open('$D')]; \
print('records',len(rs),'v2',sum(r.get(\"schema_version\")==2 for r in rs)); \
print('short_unavailable_ok', all(r.get(\"economic_pnl_unavailable\") \
for r in rs if r.get(\"shadow_side\")==\"short\")); \
print('long_uses_yes_ask', all(r.get(\"selected_exec_price_source\")==\"yes_ask\" \
for r in rs if r.get(\"shadow_side\")==\"long\" and r.get(\"would_trade\")))"
```

### 7.4 Official settlement-coverage commands

```bash
# Every resolved would_trade market should be covered in the settlement store.
python3 -c "import json; s=[json.loads(l) for l in \
open('logs/settlements.jsonl')]; \
from collections import Counter; c=Counter(x.get('resolution_status') for x in s); \
print('settlements', c)"
# Backfill + sweep are read-mostly but hit the network; a human runs these:
#   .venv/bin/python scripts/backfill_official_resolutions.py --since <ISO> --until <ISO>
#   .venv/bin/python scripts/sweep_settlements.py
```

### 7.5 Watchdog diagnostics commands

```bash
# Recent watchdog restarts + reasons, and captured diagnostics.
python3 -c "import json; e=[json.loads(l) for l in \
open('logs/runner_events.jsonl')]; \
print([(x['t'],x.get('reason')) for x in e \
if x.get('event_type')=='watchdog_restart'][-10:])"
ls -lt logs/diagnostics/stall_diag_*.json | head
# Verify the diagnostics path itself is healthy (must print RESULT: PASS):
.venv/bin/python scripts/diag_stall_smoke_test.py
```

A `heartbeat_stale` within ~1\u20132 min of a `runner_start` is the (now-fixed)
startup false-positive pattern; a `heartbeat_never_fresh` reason means a child
never produced a fresh heartbeat within a full stale window \u2014 escalate to a
human, do not raise the timeout.

**2026-07-26 update:** a second, distinct `heartbeat_stale` pattern was found
and fixed at the source \u2014 a child that hit a transient
`RemoteProtocolError("Server disconnected")` during Nautilus startup
execution-state reconciliation would sit completely inert (zero logs, zero
heartbeats) for ~45 minutes until the watchdog killed it (2 occurrences in
105 child cycles over the 2026-07-20..27 run). `bot.py` now skips that
reconciliation path entirely while `DRY_RUN=true`, so this specific trigger
should not recur \u2014 but the watchdog itself is unchanged and remains the
backstop for any *other* stall. Hermes should keep watching for
`heartbeat_stale`/`heartbeat_never_fresh` exactly as before; do not treat the
fix as a reason to relax monitoring.
### 7.6 Dual-book NO-ask capability check (read-only)

```bash
# Bounded (\u2264600s), read-only GET; proves a real NO ask is obtainable.
.venv/bin/python scripts/no_book_smoke_test.py --duration 180 --interval 30 --json
# Expect "result": "PASS" with captured_executable_no_ask set and all
# validation_flags true. In new schema-v2 records, shorts with both books ok
# should now carry a non-null selected_exec_price sourced from the NO ask.
```

This smoke test never starts the runner, `--live`, or any order path. It is the
canonical check that the NO-token executable-price capture is working. See
[PAPER_EXECUTION_MODEL.md](PAPER_EXECUTION_MODEL.md).

### 7.7 Run provenance (added 2026-07-26)

```bash
# which commit/config is the CURRENT run session actually executing (read-only)
cat logs/runs/*/run_manifest.json | python3 -c \
  "import json,sys; [print(json.loads(l)['run_id'], json.loads(l)['git']['head'][:12], json.loads(l)['git']['dirty']) for l in sys.stdin]"
```
Hermes can quote `run_id`/`git_head`/`git_dirty` from the latest
`logs/runs/<run_id>/run_manifest.json` when reporting run health — this
disambiguates which code version a given day's `shadow_decisions_*.jsonl`
records came from (the 2026-07-20..27 run had a mixed-version tail because
source files changed while the runner stayed active — see
`PROJECT_STATE.md`). Read-only only; Hermes must never write to
`logs/runs/`.