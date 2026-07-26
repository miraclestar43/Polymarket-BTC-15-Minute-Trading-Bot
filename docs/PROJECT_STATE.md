# PROJECT STATE

_Last updated: 2026-07-26 (final 7-day DRY-RUN closeout audit). DRY-RUN ONLY._

_Exact final HEAD is recorded in /home/star/polymarket-7d-final-manifest.txt._

## What this project is

A shadow/paper research harness for a BTC 15-minute Up/Down strategy on
Polymarket. It records model decisions and would-be trades but **does not place
live orders** while `DRY_RUN=true` / `LIVE_TRADING_ACK=false`.

## Current safety posture

- `DRY_RUN=true` (default in `config.py DEFAULTS`).
- `LIVE_TRADING_ACK=false` (only ever read from `.env`, never from YAML).
- `polymarket-dryrun.service` is **inactive/disabled** — untouched this session.
- No live order path is enabled.
- **Live trading remains NOT APPROVED** — see the 7-day closeout below.

## Final 7-day closeout (runner_start 2026-07-20T04:17:10.903548Z, PID 19538)

Frozen analysis snapshot under `/home/star/polymarket-7d-final-audit/`
(`manifest.txt` has SHA256 hashes of every scoped source file). The
collection service (`polymarket-collection-7d.service`) was still active at
the moment this snapshot was frozen — analysis is scoped to
`[2026-07-20T04:17:10.903548Z, 2026-07-26T16:23:41.723Z]` (~6.46 of the
intended 7 days) at the operator's explicit direction; the service itself was
never stopped, restarted, or signalled by this audit.

- **Lifecycle:** 105 child_start / 104 child_exit, 102 clean (`exit_code=0`,
  ~90 min each), **2 watchdog restarts** (both `heartbeat_stale`, both
  root-caused to the same trigger — see §"Reconciliation stall" below), 0
  nonzero exits, 0 competing runners, `DRY_RUN=true`/`LIVE_TRADING_ACK=false`
  logged at all 105 child startups, 0 `--live`.
- **Schema-v2 completeness:** 8,895 records, 100% `schema_version=2`, 0 v1, 0
  malformed. 930 would_trade rows; both books valid for 8,805/8,895 (99.0%);
  LONG (351 filled) proven 100% off the YES ask, SHORT (480 filled) proven
  100% off the NO ask — 0 YES-midpoint-as-NO substitutions across the run.
  Official settlement coverage 930/930 (100%) as of the snapshot fetch.
- **Unknown-side would_trade bug (found, root-caused, forward-fixed):** 96 of
  the 930 would_trade rows had `shadow_side="unknown"` (no side, no selected
  token), confined to the 4 Kelly-relaxed variants
  (`shadow_no_kelly`, `shadow_allow_kelly_minus_002/005`,
  `relaxed_markov_and_kelly_shadow`). Root cause:
  `core/strategy_brain/shadow_evaluator.py` only gated those variants' Kelly
  check on `shadow_direction != "blocked_by_direction"`, not on having an
  actual long/short side, so a neutral (`direction="unknown"`) real signal
  could still pass a stale/relaxed Markov+Kelly gate. **These rows were never
  counted in directional or economic metrics** (no valid side/price to use),
  so this did not contaminate any P/L or accuracy number in this report — it
  only inflated the four variants' raw `would_trade` counts. Fixed forward
  with an explicit `has_valid_side` gate; regression tests added
  (`tests/test_shadow_unknown_side_no_trade.py`); no historical record was
  rewritten.
- **Reconciliation stall (found, root-caused, fixed):** both watchdog restarts
  were the same deterministic failure: a transient
  `httpcore.RemoteProtocolError("Server disconnected")` during Nautilus's
  startup execution-state reconciliation makes
  `SystemKernel.start_async()` (`nautilus_trader/system/kernel.py`) `return`
  **before** `self._trader.start()` — the process stays alive but produces
  zero market subscriptions, quotes, or heartbeats for ~45 minutes until the
  external watchdog kills it. Reconciling real exchange order/fill state is
  meaningless while `DRY_RUN=true` (no real order is ever placed), so
  `bot.py` now passes `reconciliation=not simulation` to
  `LiveExecEngineConfig`, skipping the vulnerable path entirely in dry-run
  while leaving the safe `reconciliation=True` default for any live run.
  Static regression test: `tests/test_reconciliation_dry_run_gate.py`.
- **Cosmetic `[LIVE]` log label (found, fixed):** `execution/execution_engine.py`
  logged `Initialized Execution Engine [LIVE]` even though `self.dry_run` was
  correctly `True` (the log read the raw constructor arg, not the
  config-resolved flag). Fixed; no trading-gate behaviour changed; regression
  test `tests/test_execution_engine_mode_label.py`.
- **Corrected results (pre-fee, $1 fixed risk, View B):** baseline n=36, net
  +$4.09, ROR 11.4%, stays positive through a hypothetical 2% fee (+$3.37,
  9.4%). `shadow_no_kelly` n=364, net +$8.29 pre-fee but ROR collapses to
  0.28% at a hypothetical 2% fee, and its **paired, market-clustered
  bootstrap incremental P/L vs baseline crosses zero** (mean +0.012, 95% CI
  [-0.015, 0.036]) — its large raw P/L is **not** distinguishable from
  baseline once markets are not double-counted across variants. LONG
  consistently outperforms SHORT in both (baseline ROR 23.0% vs 4.0%;
  shadow_no_kelly 3.5% vs 1.2%). No variant meets the pre-registered evidence
  bar (§ below) — most are far short of n=300, and there is no true
  out-of-sample (separate-week) confirmation yet.
- **No strategy edge is claimed.** See `RESEARCH_FINDINGS.md` for full detail
  and `NEXT_AGENT_TASKS.md` for the recommended next experiment (a second,
  unmodified 7-day window — not a parameter change).

## New-run audit (2026-07-19 dry-run, schema-v2 live)

A fresh dry-run was audited over an explicit window
**2026-07-19T05:19:21Z → 15:13:03Z** (runner_start pid 10333, `bot_args=[]` —
confirms no `--live`). The window was selected from `runner_events.jsonl`, not
`--latest-run`. The runner self-restarts clean children ~every 90 min and was
still running at audit time; it was **not** killed (safe dry-run).

- **615** shadow records, **all `schema_version: 2`**; 61 `would_trade` across
  variants; 41 unique markets.
- **Schema-v2 quote capture verified:**
  - **LONG (BUY YES)** selects the YES token and uses the **YES ask** as the
    executable entry (`selected_exec_price_source="yes_ask"`) — economic P/L
    **available**.
  - **SHORT (BUY NO)** does **not** reuse the YES price. The NO book was not
    subscribed, so `selected_exec_price=None` and
    `economic_pnl_unavailable=True` (`no_token_ask_not_captured`). This is
    honest — no YES-as-NO substitution, no midpoint guess.
  - Two derivable completeness gaps were closed at source: `condition_id` and
    `market_end_time` are now populated from data already in the record (no
    network, no strategy impact) for **future** runs.
- **Forensic scan of all 615 records:** zero anomalies across the 12 checked
  failure modes (short-uses-yes, price out of (0,1), quote-after-decision,
  stale quote, decision-after-close, exec==p_model, etc.). Per-trade CSV:
  `logs/forensic_v2_run.csv` (61 trades, 0 anomaly flags, 39 unavailable /
  22 executable). Quotes are contemporaneous (quote-age ≈ 0s), all decisions
  ~2 min before close.
- **Settlement:** 23/23 `would_trade` markets officially resolved and swept
  (store now 751 resolved, 0 inferred, 0 unavailable).
- **Three separated result views** (`logs/post_run_report.md`):
  - **A directional** — accuracy 1.0 across variants but **tiny n** (baseline
    n=3, shadow_no_kelly n=23, short_only n=2, long_only n=1); **not a
    statistically meaningful edge**.
  - **B executable economic** — now computed **only** from records with a valid
    selected-token ask (LONG). e.g. shadow_no_kelly: 10 executable longs,
    +$0.95 net at $1 fixed risk, ROR 9.5%, entry 0.63–0.99. **SHORT stays
    UNAVAILABLE** (no NO ask) and is never priced off YES.
  - **C unavailable** — 39/61 records lack an executable ask (all shorts +
    no-trades); reported explicitly, never as synthetic P/L.
- **No new economic edge established:** LONG executable economics are now
  capturable but n is tiny, and all SHORT economics remain unavailable this run.

### Watchdog startup false-positive (found + fixed)

Exactly **one** `watchdog_restart` (`heartbeat_stale`) fired at
**05:20:24Z, ~63 s after start** — impossible for a real 45-min stale window.
Root cause **proven**: the heartbeat staleness check was the only liveness
check **not scoped to `run_start_ts`** (journal/shadow already were), so it
compared against a **previous run's** stale heartbeat file before the child
wrote its first fresh heartbeat. The captured `stall_diag` shows empty
`recent_ops`/`loop_lags`/`asyncio_tasks` and the main thread in
`run_until_complete(run_async())` startup — **no blocking operation**.

Fix (operational, **not** a timeout increase, **not** strategy logic): scope
the heartbeat check to `run_start_ts` and add a startup grace that still
restarts a genuinely wedged startup (`heartbeat_never_fresh`) after a full
stale window. Diagnostics smoke test still **PASS** (SIGUSR1 faulthandler,
SIGUSR2 rich dump, secrets redacted, dir writable).

## NO-token executable-price capture (2026-07-19, this session)

The last missing end-to-end capability — capturing a **real, contemporaneous
NO-token executable ask** — is now implemented and proven live. No strategy
logic changed (Kelly/Markov/thresholds/side/sizing/probabilities untouched);
`DRY_RUN=true` / `LIVE_TRADING_ACK=false` throughout; no runner started.

- **Root cause of the old gap:** the bot subscribed only to the YES/Up
  instrument; the NO instrument was stored but never subscribed, so SHORT
  economics were (honestly) `economic_pnl_unavailable`.
- **Dual-book capture:** new `core/market_data/dual_book.py::DualBookStore`
  keyed **by token id**, subscribed to **both** books in `bot.py`, routes ticks
  by token id, invalidates on reconnect (generation bump), rejects stale/
  crossed/out-of-range/cross-market data.
- **Realistic paper execution:** new `core/strategy_brain/paper_execution.py`
  walks the observed ask ladder for a fixed-risk stake and records VWAP fill,
  partial/unavailable status, and pre-fee/fee labelling — never fabricating
  liquidity.
- **Schema v2 extended additively** in `shadow_evaluator._forward_schema_fields`
  (per-token sizes/timestamps/ages, cross-book skew, book status/generation,
  validation flags, `paper_*` fill fields, `executable_economic_available`).
- **Live smoke test PASS:** `scripts/no_book_smoke_test.py` captured a real NO
  ask with both books `ok` and all validation flags healthy.
- **Resolution store rebuilt safely** with the hardened parser via
  `scripts/rebuild_official_resolutions.py` (backup kept; active store replaced
  only on full validation).
- **Tests:** full suite **306 passed** (263 baseline + 43 new deterministic
  tests); preflight **OK**.

## Full-runner dual-book audit (2026-07-19/20)

A full 4-hour dry-run then exercised the dual-book code in the real runner and
was audited end-to-end (`scripts/dual_book_run_audit.py`).

- **Window:** `2026-07-19T21:16:00Z → 2026-07-20T01:16:10Z` (runner_start pid
  17409, `bot_args=[]`), 4.00h, 3 children (2 planned ~90-min restarts),
  **0 watchdog restarts** — the startup-heartbeat fix held (no false positive).
- **Dual-book capture:** 240 records / 16 markets, **both books `ok` for 100%**,
  NO/YES quote-age median ~0.23s (max 2.5s, all < 60s), skew max 0.107s,
  **0 stale / 0 skew / 0 spread / 0 crossed**.
- **Pricing proof:** 16 would-trades all **LONG**, each using the **YES ask**
  (16/16, all 11 forensic proof checks pass). No SHORT would-trades, but **all 98
  short-side evaluations priced off the NO ask** (0 off the YES midpoint).
- **Settlement:** 4/4 would-trade markets officially resolved (3 UP, 1 DOWN),
  hardened parser, cid match, single winner; appended to the official store
  (backup kept).
- **Economics (view B, $1 fixed risk, pre-fee):** net negative, tiny n — entry
  0.90–0.996 so wins pay pennies vs a full-stake loss. **No edge; descriptive.**
- **L1 limitation:** live feed is top-of-book only (`depth_level_count`=1 for all
  fills); multi-level VWAP not observed live; no fabricated depth.
- **Preflight hardened:** added disk-space, NTP clock-sync, repo/venv, dual-book
  config, freshness/skew, and store-readability checks — all PASS.
- **Tests:** full suite **314 passed** (+8 audit/preflight regression tests);
  synthetic stall smoke test **PASS**.
- Artifacts: `logs/dual_book_run_forensic.csv`, `logs/dual_book_run_audit.json`;
  backups `polymarket-final-code.bundle` + `polymarket-final-data.tar.gz` +
  `polymarket-final-manifest.txt`.

## Headline finding (CRITICAL)

The historical "profitability" was **an artifact of a pricing bug**, not real
edge. `entry_price` in shadow records is the **YES-token midpoint**
`(bid+ask)/2` for **both** long and short trades. Settlement used that value
directly as the *selected token's* cost, so **SHORT (BUY NO) trades priced the
NO token using the YES price**. At low YES prices this fabricated enormous
payouts.

- Proven via the `odds_b` invariant: **1531/1531 records, 0 mismatches**.
- **Historical executable P/L is NOT recoverable** — only midpoints were stored,
  and no NO-token quote or quote timestamp was ever recorded.
- Directional mapping is correct; official resolution matched settled outcome
  **1757/1757**; no look-ahead (decision_ts < market_end 1757/1757).

### Corrected results (synthetic, fixed-risk R=$1, NO=1−YES midpoint)

| variant          | n    | win%  | BUGGY P/L   | CORRECTED (midpoint) |
|------------------|------|-------|-------------|----------------------|
| baseline         | 67   | 68.7% | $45.02      | ~$1.24               |
| shadow_no_kelly  | ~673 | 93.0% | ~$33,756    | ~$14.45              |
| short_only       | 41   | 63.4% | $41.48      | ~-$2.30              |
| long_only        | 26   | 76.9% | $3.54       | $3.54 (unchanged)    |

Only **directional win rates** and **long-only P/L** survive as valid. All
recorded economic P/L for baseline / short / shadow_no_kelly is **invalidated**.

## What changed this session (code)

- Fixed the pricing bug at source in `core/strategy_brain/economics.py` and
  `core/strategy_brain/settlement.py` (derive the selected-token exec price by
  side; SHORT prices NO = 1−YES).
- Shadow schema **v2**: a self-describing `executable_quote` block with both
  YES/NO book fields, selected token id, quote timestamp/source, and an
  explicit `economic_pnl_unavailable` flag for historical records.
- Official-resolution parser hardened (both token ids, `winning_price`,
  `tokens_summary`, `parser_version`; price==1 alone is insufficient).
- Diagnostics: redaction hardened (URL query strings, Bearer tokens, PEM);
  deterministic no-network synthetic-stall smoke test.

## Test status

`.venv/bin/python -m pytest tests/ -q` → **263 passed** (was 252; +11 regression
tests for schema-v2 completeness, executable-quote derivation, view-B
economics, and the watchdog startup false-positive).

## Open risks / not done

- **SHORT economics still unavailable:** the NO order book is not subscribed,
  so no executable NO ask is captured. Every SHORT remains
  `economic_pnl_unavailable` and is never priced off YES. Capturing the NO book
  is the single biggest gap before any short economic conclusion is possible.
- **Sample size is far too small** for an economic or directional edge claim
  (per-variant n in low single/double digits over ~10 h).
- Forward LONG economic P/L is now capturable (schema v2, `yes_ask`), but is an
  optimistic upper bound (ask fill, no extra fee/slippage modelled).
- The on-disk `official_resolutions.jsonl` store's **older** records were built
  by the pre-`clob-resolution-2` parser (no `losing_token_id`/`tokens_summary`);
  new-run records are fully parsed.

## Recommended next steps (research only)

- **Next dry-run duration:** run continuously for **at least 5\u20137 days**
  (~480\u2013670 fifteen-minute cycles) to accumulate a usable per-variant n, and
  **subscribe the NO book** so SHORT executable economics become available.
- **Minimum evidence before any strategy change:** a pre-registered hypothesis
  plus **\u2265 300 resolved decisions per variant** with valid executable quotes,
  a directional accuracy CI that excludes 0.5, **and** a positive view-B ROR
  after modelled fees/slippage \u2014 across an out-of-sample window. Directional
  accuracy alone is insufficient.

## Live trading status

**Live trading is NOT approved.** `DRY_RUN` must remain `true` and
`LIVE_TRADING_ACK` `false`. No service is enabled and no order path is live.

See `RESEARCH_FINDINGS.md` for the full audit and `DATA_SCHEMA.md` for the
field-by-field data model.
