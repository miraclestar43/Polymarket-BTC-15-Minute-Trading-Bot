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
