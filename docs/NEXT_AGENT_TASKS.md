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

- Branch `main`. The exact final HEAD after the 2026-07-26 closeout (audit
  tooling, 3 bug fixes, tests, docs) is recorded in
  `/home/star/polymarket-7d-final-manifest.txt`. Pre-closeout HEAD was
  `4c9da8d`. Commits are **not pushed** — the exact push command is in that
  manifest, not executed.

## 3. Valid vs invalid historical conclusions

- **Valid:** directional mapping (Up/Down); official resolution matching; absence
  of look-ahead; LONG executable economics (YES is the selected token).
- **Invalid / not recoverable:** all historical *recorded* economic P/L that used
  `entry_price` (a YES midpoint) as the selected-token cost — this fabricated
  SHORT payouts. Historical executable SHORT economics are **not recoverable**
  (no NO quote/timestamp was stored) and must stay reported as *unavailable*.

## 4. Result of the FINAL 7-day closeout (2026-07-20 → 2026-07-26)

- Window: `2026-07-20T04:17:10.903548Z → 2026-07-26T16:23:41.723Z`
  (runner_start pid 19538, `bot_args=[]`), ~6.46 of the intended 7 days,
  frozen at the operator's explicit direction (service was not stopped).
  Frozen scoped snapshot + hashes: `/home/star/polymarket-7d-final-audit/`.
- 105 child cycles (102 clean ~90-min restarts, **2 watchdog restarts**, both
  root-caused and fixed — see §12 below). 8,895 schema-v2 records, 930
  would_trade rows, both books valid 99.0%, LONG (351 filled) 100% off the
  YES ask, SHORT (480 filled) 100% off the NO ask, official settlement
  coverage 930/930 at fetch time.
- Directional (Wilson 95% CI): baseline n=36, 75.0% [58.9%, 86.3%];
  shadow_no_kelly n=367, 92.9% [89.8%, 95.1%].
- Executable economics (View B, $1 fixed risk, pre-fee): baseline net
  **+$4.09**, ROR **11.4%**, stays positive through a hypothetical 2% fee
  (9.4%). shadow_no_kelly net +$8.29, ROR 2.3%, collapsing to 0.3% at a
  hypothetical 2% fee. **shadow_no_kelly's incremental P/L over baseline,
  bootstrapped with market-clustered resampling, has a 95% CI of
  [-0.015, +0.036] per trade — crosses zero.** LONG outperforms SHORT in
  every variant checked (baseline ROR 23.0% vs 4.0%).
- Full detail (per-variant tables, fee sweep, paired comparisons, day-by-day
  P/L, calibration bins): `RESEARCH_FINDINGS.md` §10.
- **No strategy edge is established. No variant meets the §8 evidence bar.**

## 5. Remaining L1 depth limitation

- The live Nautilus quote feed is **top-of-book (L1) only**. Across the full
  7-day closeout (831 valid-executable fills), exactly **one** real partial
  fill occurred (2026-07-25, LONG, requested 1.136 shares vs 0.74 observed
  top-of-book size) — the first live confirmation of the `stake_exceeded_top_level`
  path. Multi-level VWAP remains implemented/unit-tested but not observed
  live. Deeper liquidity is never fabricated.

## 6. Fee limitation

- No fee rate is exposed by the current feed, so every paper fill is labelled
  **`pre_fee`** (`fee_rate=None`). All economic numbers are **pre-fee**; real net
  P/L will be lower. Do not present pre-fee numbers as net-of-fee.

## 7. Recommended NEXT experiment (single, primary — see Phase 10 rationale)

**Do not change strategy logic for this next run.** The single most
scientifically load-bearing next step is a **second, independent, unmodified
7-day DRY-RUN window** — a true out-of-sample replication, not a new
variant:

- **Hypothesis:** baseline's week-1 pre-fee executable ROR (+11.4%,
  directional accuracy 75.0%) and its LONG≫SHORT asymmetry (23.0% vs 4.0%
  ROR) replicate in an independent week.
- **Control:** current baseline parameters, unchanged (this is a replication
  run, not a treatment).
- **Treatment:** none — no Kelly/Markov/threshold/side/sizing change.
- **Metrics:** per-variant directional accuracy + Wilson CI; View-B pre-fee
  AND fee-swept (0/0.25/0.5/1/2%) ROR; paired bootstrap incremental P/L vs
  baseline clustered by `condition_id`; day-by-day cumulative P/L; entry-price
  concentration — identical methodology to `RESEARCH_FINDINGS.md` §10 so the
  two windows are directly comparable.
- **Minimum sample:** ≥300 resolved valid-executable decisions for
  `shadow_no_kelly` (the highest-n variant) pooled across both weeks before
  any edge claim; ≥60 for baseline/long_only/short_only individually
  (double week 1) as an interim checkpoint.
- **Stop criteria:** run the full 7 days unattended, exactly like week 1. Do
  **not** stop early on interim P/L (avoids early-stopping/p-hacking bias);
  the only valid early-stop reasons are the same safety/data-integrity
  defects that would justify stopping any run.
- **Out-of-sample requirement:** week 2 IS the out-of-sample test of week 1's
  findings — do not draw a conclusion from week 1 alone (already true here).
- **Decision rule after week 2:** only consider a strategy-parameter change
  if, pooling weeks 1+2, a variant has (a) ≥300 resolved valid-executable
  decisions, (b) directional Wilson CI excluding 0.5, (c) View-B ROR positive
  after a conservative 1% fee assumption, **and** (d) week 2 directionally
  replicates week 1 (same sign of ROR, LONG still ≥ SHORT). If any condition
  fails, run a third window rather than changing strategy. **Live trading
  remains NOT APPROVED regardless of outcome** — that requires a separate,
  explicit, human-reviewed decision outside this research loop.

Exact command (NOT executed by this closeout — run only after human
approval; uses a fresh unit name so it cannot collide with the still-running
week-1 unit):

```bash
cd /home/star/polymarket-BTC-15-Minute-Trading-Bot
.venv/bin/python scripts/preflight_dryrun.py          # expect RESULT: OK
systemd-run --user --unit=polymarket-collection-7d-w2.service \
  --working-directory=/home/star/polymarket-BTC-15-Minute-Trading-Bot \
  --property=RuntimeMaxSec=7d \
  /home/star/polymarket-BTC-15-Minute-Trading-Bot/.venv/bin/python \
  /home/star/polymarket-BTC-15-Minute-Trading-Bot/15m_bot_runner.py
# let it run the full 7 days (self-restarts clean children ~every 90 min)
```
Stop (only if ever needed, and only with approval):
`systemctl --user stop polymarket-collection-7d-w2.service`.
Audit afterwards with the exact window from `logs/runner_events.jsonl`
(never `--latest-run` without first proving it matches):
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

## 12. Three proven, fixed, non-strategy defects (2026-07-26 closeout)

1. **Unknown-side would_trade** (`core/strategy_brain/shadow_evaluator.py`):
   the 4 Kelly-relaxed variants could set `would_trade=true` with
   `shadow_side="unknown"` (no side, no price). Never affected economics
   (already excluded downstream); fixed forward with a `has_valid_side` gate.
   Tests: `tests/test_shadow_unknown_side_no_trade.py`.
2. **Execution reconciliation stall** (`bot.py` / vendored `nautilus_trader`):
   a transient exchange disconnect during startup reconciliation aborted the
   whole bot startup, causing a ~45-minute inert stall recovered only by the
   watchdog (2/105 child cycles). Fixed by skipping reconciliation while
   `DRY_RUN=true`. Tests: `tests/test_reconciliation_dry_run_gate.py`.
3. **Cosmetic `[LIVE]` log label** (`execution/execution_engine.py`): fixed to
   read the effective `self.dry_run` instead of the raw constructor arg.
   Tests: `tests/test_execution_engine_mode_label.py`.

None of these changed Kelly, Markov, thresholds, side selection, model
probabilities, or sizing. Full pytest suite: 325 passed (was 314).

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
