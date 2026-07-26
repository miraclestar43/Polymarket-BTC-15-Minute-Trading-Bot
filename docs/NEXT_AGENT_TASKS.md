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

- Branch `main`. The exact HEAD after the 2026-07-26 provenance-hardening
  session (run provenance, replication preflight, tests, docs) is recorded
  in `/home/star/polymarket-w2-preflight-manifest.txt`. That session built
  on the closeout HEAD recorded in `/home/star/polymarket-7d-final-manifest.txt`
  (itself built on pre-closeout HEAD `4c9da8d`). Commits are **not pushed** —
  the exact push command is in the newer manifest, not executed. **Week 2
  must start from this exact HEAD** (or a later descendant), with a clean
  working tree (`--require-clean-git`), and must not change commits for its
  entire 7-day duration.

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
7-day DRY-RUN window run at one fixed Git commit for its entire duration**
— a true out-of-sample replication, not a new variant. Week 1
(`2026-07-20..27`) had source files change mid-run because the runner was
never stopped while this session's bug fixes were written; Week 2 must not
repeat that ambiguity (see `PROJECT_STATE.md` §"Post-freeze tail" and
§"Run provenance"). Confirm with `--require-clean-git` preflight (§7a)
before starting, and do not touch the repo for the full 7 days once started.

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

## 7a. Week-2 acceptance criteria (define BEFORE starting, per replication discipline)

**Data integrity**
- 100% of records at the current forward schema (`schema_version=2` plus
  the `run_id`/`git_head`/`strategy_config_sha256` provenance fields).
- ≥98% both-book availability.
- ≥95% official settlement coverage after a final backfill sweep.
- Zero YES-midpoint-as-NO reuse (already structurally impossible — proven
  0/0 across Week 1).
- Zero selected-token/side mismatch.
- Zero `would_trade=true` with `shadow_side="unknown"` (the forward fix is
  in effect for the whole run since it starts at one fixed, already-fixed
  commit — unlike Week 1, there should be **no** such records at all).
- Every executable P/L record has a contemporaneous selected-token ask.
- L1 depth limitation explicitly retained and labelled, never silently
  assumed away.
- **Exactly one `git_head` value across every record in the run** (verify
  this first — it is the entire point of the provenance fields).

**Reliability**
- No reconciliation-induced dry-run stall (the fix should make this class of
  watchdog restart disappear entirely — confirm via `runner_events.jsonl`).
- Every watchdog event (if any) investigated with the same rigor as Week 1's
  two.
- Every planned child restart resubscribes both books with no stale carry-over.
- No competing runner at any point (`polymarket-collection-7d.service` must
  be fully stopped before Week 2 starts).
- No mixed Git commits within the run (see data-integrity bullet above).

**Research**
- No edge conclusion from raw P/L or raw n alone.
- Market-clustered paired uncertainty (bootstrap by `condition_id`, not by
  variant-row).
- First-half vs second-half reporting within Week 2.
- Week-1 vs Week-2 out-of-sample comparison (§7 primary comparisons).
- Hypothetical fee sensitivity (0/0.25/0.5/1/2%), clearly labelled
  hypothetical.
- P/L concentration analysis (top-1/5/10).
- No live-trading recommendation, regardless of result.

**A technically successful run does NOT require positive P/L** — data
completeness, reliability, and methodology are the pass/fail bar, not
profitability.

## 7b. Exact commands (NOT executed by this task — run only after human approval)

```bash
cd /home/star/polymarket-BTC-15-Minute-Trading-Bot
.venv/bin/python scripts/preflight_dryrun.py --require-clean-git
# expect RESULT: OK — a dirty tree BLOCKS (exit 1); commit or stash first.
```
Start (uses a fresh unit name so it cannot collide with the Week-1 unit;
`RuntimeMaxSec=7d`; not a persistent/enabled service, does not auto-restart
after completion — `Restart=no` is the systemd-run default):
```bash
systemd-run --user --unit=polymarket-collection-7d-w2.service \
  --working-directory=/home/star/polymarket-BTC-15-Minute-Trading-Bot \
  --property=RuntimeMaxSec=7d \
  /home/star/polymarket-BTC-15-Minute-Trading-Bot/.venv/bin/python \
  /home/star/polymarket-BTC-15-Minute-Trading-Bot/15m_bot_runner.py
# let it run the full 7 days untouched (self-restarts clean children ~every 90 min)
```
Stop (only if ever genuinely needed, and only with approval):
```bash
systemctl --user stop polymarket-collection-7d-w2.service
```
Audit afterwards with the exact window from `logs/runner_events.jsonl` and
`logs/runs/<run_id>/run_manifest.json` (never `--latest-run` without first
proving it matches):
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
probabilities, or sizing.

## 13. Run provenance + replication preflight (2026-07-26, this session)

Added `core/diagnostics/run_provenance.py` (Git HEAD/branch/dirty, config
hashes, run_id — computed once per run session, propagated to children via
env vars and into shadow records; never per-record, never a secret) and
`logs/runs/<run_id>/run_manifest.json` (+ `run_manifest_end.json`).
`scripts/preflight_dryrun.py` gained Git-state/config-hash/competing-runner/
collection-service checks and `--require-clean-git`. This exists specifically
so Week 2 cannot repeat Week 1's post-freeze mixed-version tail (§4, §"Post-freeze
tail" in `PROJECT_STATE.md`). No strategy logic changed. Full pytest suite:
354 passed (was 325 after the prior closeout, 314 before that).

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
