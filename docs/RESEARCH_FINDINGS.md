# RESEARCH FINDINGS

_Forensic audit of the BTC 15-min Up/Down shadow strategy. DRY-RUN ONLY._

## 1. The red flag that started this

A strategy that appears to buy the eventually-winning binary token below $0.05
hundreds of times at a 93% win rate is economically implausible unless the
recorded entry price is not the selected token's executable price. That
suspicion was **confirmed**.

## 2. Proven root-cause bug

`entry_price` in shadow records is the **YES-token midpoint** `(bid+ask)/2` for
**both** long and short trades. Settlement used it directly as the *selected
token's* cost, so **SHORT (BUY NO) trades used the YES price as the NO cost**.

**Proof — the `odds_b` invariant.** For every record, `odds_b` equaled the
value implied by treating `entry_price` as the YES midpoint:

- long:  `odds_b == (1 − e) / e`
- short: `odds_b == e / (1 − e)`

Result: **1531/1531 records match, 0 mismatches.** This is a deterministic
signature, not a correlation.

The largest "winners" were shorts at YES ≈ 0.0015 (markets ~99.85% certain
DOWN). The bug valued the NO position as if it cost $0.0015 instead of ~$0.9985,
inflating a ~1.0015× return into a ~665× return.

## 3. What is and isn't valid

| Property                         | Verdict |
|----------------------------------|---------|
| Directional mapping (Up/Down)    | **Correct** |
| Official resolution vs settled   | **1757/1757 match** |
| Look-ahead in decisions          | **None** (decision_ts < market_end 1757/1757; entry is a live YES mid) |
| Recorded economic P/L (baseline / short / shadow_no_kelly) | **INVALIDATED** |
| Directional win rates            | Valid |
| long_only P/L                    | Valid (unaffected — YES *is* the selected token) |
| Historical executable economic P/L | **Not recoverable** (only midpoints stored; no NO quote/timestamp) |

## 4. Corrected numbers

Buggy vs corrected (synthetic, fixed-risk R=$1, NO = 1−YES midpoint):

| variant          | n    | win%  | BUGGY P/L | CORRECTED |
|------------------|------|-------|-----------|-----------|
| baseline         | 67   | 68.7% | $45.02    | ~$1.24    |
| shadow_no_kelly  | ~673 | 93.0% | ~$33,756  | ~$14.45   |
| short_only       | 41   | 63.4% | $41.48    | ~-$2.30   |
| long_only        | 26   | 76.9% | $3.54     | $3.54     |

`shadow_no_kelly` directional accuracy 95% Wilson CI ≈ [0.908, 0.947]. The
corrected midpoint numbers are a **sensitivity estimate**, not realizable P/L —
real fills require executable asks and account for spread/slippage/fees.

## 5. Three strictly-separated evaluations

Produced by `scripts/corrected_conclusions.py`:

- **A — Directional:** accuracy + Wilson CIs (long/short/balanced). Valid.
- **B — Recorded executable economic:** status **unavailable** for all
  historical records (no valid executable selected-token quote was stored).
- **C — Synthetic / sensitivity:** midpoint-based, fixed-risk, clearly labelled
  as non-executable.

## 6. Fixes applied

- Corrected pricing at source (`economics.py`, `settlement.py`): the selected
  token's exec price is derived by side; SHORT prices NO = 1−YES.
- Shadow schema v2 records the full executable-quote block and an explicit
  `economic_pnl_unavailable` flag (see `DATA_SCHEMA.md`).
- Official-resolution parser hardened against the Gamma `data[0]` wrong-market
  class of bug (condition-id match required; `price==1` alone insufficient).
- Regression tests lock in every fix (263 tests pass).

## 6b. New-run audit (2026-07-19 dry-run, schema v2 live)

Window **2026-07-19T05:19:21Z → 15:13:03Z** (explicit, from
`runner_events.jsonl`; not `--latest-run`). 615 records, all `schema_version 2`.

- **SHORT is now honest:** with no NO book subscribed, shorts record
  `selected_exec_price=None`, `economic_pnl_unavailable=True`
  (`no_token_ask_not_captured`) — the YES price is **never** reused as the NO
  cost. This is the fixed behaviour, observed live.
- **LONG is executable:** longs select the YES token and pay the **YES ask**
  (`selected_exec_price_source="yes_ask"`), so view-B economics exist for them.
- **Forensic scan of all 615 records:** 0 anomalies across 12 failure modes
  (`logs/forensic_v2_run.csv`, 61 trades, 0 flags). Quotes contemporaneous;
  decisions ~2 min before close; settlement 23/23 resolved.
- **Three separated views** (`logs/post_run_report.md`): A directional accuracy
  1.0 but **tiny n** (not meaningful); B executable economics available for
  LONG only (e.g. shadow_no_kelly 10 longs, +$0.95 at $1 risk, ROR 9.5%),
  SHORT unavailable; C the unavailable set reported explicitly.
- **No new edge established.** LONG economics are now capturable but n is tiny
  and all SHORT economics remain unavailable (NO book not captured).
- **Watchdog:** one `heartbeat_stale` at +63 s was a **proven startup false
  positive** (unscoped heartbeat check vs a prior-run heartbeat); fixed by
  scoping to `run_start_ts` with a startup grace. `stall_diag` showed no
  blocking operation.

## 7. Recommendation

Do **not** treat any historical economic P/L as evidence of edge. Only
directional accuracy is established. A forward DRY-RUN with schema v2 is required
to gather **executable** NO-side quotes before any economic conclusion — and
before any consideration of live trading.

## 8. NO-book capture closed (2026-07-19)

The blocker in §7 — no executable NO quote — is now resolved at the source.
Both outcome order books are subscribed and maintained by token id
(`DualBookStore`); a SHORT now prices off the **observed NO ask** (never the YES
midpoint, never `1 − yes_mid`, never a closed-market quote), and a realistic
VWAP paper fill against observed ask depth is recorded. A live read-only smoke
test captured a real NO ask (both books `ok`, all validation flags healthy),
proving the capability end-to-end.

This is an **instrumentation** fix, not a new edge: strategy logic is unchanged
and no forward economic run was collected in this session. A fresh DRY-RUN is
still required to accumulate executable SHORT economics before any conclusion.
See [PAPER_EXECUTION_MODEL.md](PAPER_EXECUTION_MODEL.md).

## 9. Full-runner dual-book audit (2026-07-19/20)

Window `2026-07-19T21:16:00Z → 2026-07-20T01:16:10Z` (runner_start pid 17409),
4.00h, 0 watchdog restarts. 240 records / 16 markets; both books `ok` for 100%;
quote ages median ~0.23s (all < 60s); skew max 0.107s; 0 stale/skew/spread/crossed.

- **Dual-book pricing verified live in the real runner:** all 98 short-side
  evaluations priced off the **NO ask** (0 off the YES midpoint, selected_token==
  no_token 98/98); all 126 long-side evaluations used the **YES ask**.
- 16 would-trades, **all LONG** (up-trending window); all 11 forensic proof
  checks pass (`logs/dual_book_run_forensic.csv`).
- Settlement 4/4 markets resolved (3 UP, 1 DOWN), hardened parser.
- **View B executable economics (pre-fee, $1 risk): net negative, tiny n.** Entry
  prices 0.90–0.996 → wins pay pennies, a loss costs the whole stake. **No edge
  claimed** — descriptive statistics with Wilson CIs only.
- **L1 limitation:** live feed top-of-book only; multi-level VWAP not observed
  live; no depth fabricated. **Fees:** none exposed → all numbers **pre-fee**.

Conclusion unchanged: only directional mapping and capture correctness are
established. No economic edge. A 5–7 day dry-run is required (see
[NEXT_AGENT_TASKS.md](NEXT_AGENT_TASKS.md)).

## 10. Final 7-day closeout (2026-07-20 → 2026-07-26, frozen snapshot)

Window `2026-07-20T04:17:10.903548Z → 2026-07-26T16:23:41.723Z` (runner_start
pid 19538, `bot_args=[]`), ~6.46 of the intended 7 days (the operator elected
to close out at this point rather than wait for the scheduled boundary; the
collection service was not stopped by this audit). Full detail, hashes, and
the frozen scoped source files are under `/home/star/polymarket-7d-final-audit/`.

**Lifecycle:** 105 child cycles, 102 clean ~90-min restarts, 2 watchdog
restarts (both `heartbeat_stale`, both root-caused to the same reconciliation
stall — §13). 8,895 schema-v2 records, 930 would_trade rows, both books valid
99.0%, official settlement coverage 930/930 at snapshot-fetch time.

### View A — directional (Wilson 95% CI, informational only)

| variant | n | accuracy | 95% CI |
|---|---|---|---|
| shadow_no_kelly | 367 | 92.9% | [89.8%, 95.1%] |
| baseline / min_prob_082 / min_prob_084 / markov_10 / markov_15 / block_long_macro_bear / short_bias_macro_bear / side_specific_threshold | 36 | 75.0% | [58.9%, 86.3%] |
| shadow_allow_kelly_minus_002 | 40 | 75.0% | [59.8%, 85.8%] |
| shadow_allow_kelly_minus_005 | 41 | 75.6% | [60.7%, 86.2%] |
| relaxed_markov_and_kelly_shadow | 40 | 75.0% | [59.8%, 85.8%] |
| short_only | 22 | 72.7% | [51.9%, 86.9%] |
| trend_guard_shadow | 22 | 86.4% | [66.7%, 95.3%] |
| long_only | 14 | 78.6% | [52.4%, 92.4%] |

### View B — executable paper economics (pre-fee, $1 fixed risk)

- baseline: n=36, net **+$4.09**, ROR **11.4%** — LONG n=14 ROR 23.0%, SHORT
  n=22 ROR 4.0%.
- shadow_no_kelly: n=364, net **+$8.29**, ROR **2.3%** — LONG n=172 ROR
  3.5%, SHORT n=192 ROR 1.2%. ~80% of its valid trades sit at entry price
  ≥0.83 (near-certain favorites); P/L is well distributed (top-10 of 364
  trades = 16.7% of total |P/L|), unlike baseline's concentrated top-10 of 36
  = 45.5%.
- All other variants: n=22–41, ROR 4.0%–26.5%, mostly identical to baseline
  over this window (their parameter changes never bound).

### Fee sensitivity (hypothetical sweep, 0/0.25/0.5/1/2% — NOT actual fees)

| variant | 0% ROR | 1% ROR | 2% ROR |
|---|---|---|---|
| baseline | 11.4% | 10.4% | 9.4% |
| shadow_no_kelly | 2.3% | 1.3% | **0.3%** |
| trend_guard_shadow | 26.5% | 25.5% | 24.5% |

`shadow_no_kelly`'s thin per-trade edge is fee-fragile; baseline and
`trend_guard_shadow` stay comfortably positive through 2%.

### Paired, market-clustered comparison vs baseline

Bootstrap resamples **condition_id clusters**, not variant-rows, so the same
market shared across 15 variants is never counted as 15 independent
observations (see rule against pseudo-replication). `block_long_macro_bear`,
`short_bias_macro_bear`, `min_prob_082/084`, `markov_window_10/15`,
`side_specific_threshold` were **identical** to baseline this window (their
gates never bound). `shadow_no_kelly` added 331 trades over baseline with
mean incremental P/L +$0.012/trade but **95% CI [-0.015, +0.036] — crosses
zero**. `shadow_allow_kelly_minus_002/005` and `relaxed_markov_and_kelly`
added 4–5 trades each, incremental CIs also cross zero. **No variant's
incremental contribution over baseline is statistically distinguishable from
zero in this sample** — large raw n or raw P/L (rule: never rank by these
alone) does not itself establish an edge.

### Regime/day/half-window robustness

Day-by-day cumulative pre-fee P/L (baseline) rose most days but had one down
day (2026-07-21, -$1.41) and one partial-day dip (2026-07-26, -$0.61); still
net positive across the full window. First-half vs second-half ROR: baseline
26.3% → 3.9% (same sign, large magnitude drop — not a clean out-of-sample
replication); shadow_no_kelly 3.1% → 1.6% (more stable). LONG > SHORT holds
in both halves and both size classes.

### 13. Execution reconciliation stall — 2/105 child cycles

Both watchdog restarts (PIDs 29743 on 2026-07-22, 48947 on 2026-07-26) show
the identical signature: `httpcore.RemoteProtocolError("Server disconnected")`
during startup `generate_order_status_reports`/`generate_fill_reports` calls,
then `ExecEngine: Applying post-reconciliation startup delay (10.0s)` as the
**last** log line before ~45 minutes of total silence, then the watchdog's
`SIGTERM`. Traced into `nautilus_trader/system/kernel.py:start_async()`:
`if not await self._await_execution_reconciliation(): return` — the whole
startup aborts (never reaches `self._trader.start()`), yet the asyncio event
loop keeps spinning (the background continuous-reconciliation task's
`asyncio.sleep`/loop), which is why the process doesn't exit on its own.
Fixed by skipping reconciliation entirely while `DRY_RUN=true` (see
`PROJECT_STATE.md`); the watchdog remains the safety net for any future,
different stall.

### Unknown-side would_trade — 96/930 rows, root-caused, forward-fixed

See `PROJECT_STATE.md` and `tests/test_shadow_unknown_side_no_trade.py`. Not
a pricing or safety defect — confined to raw `would_trade` labeling on 4
Kelly-relaxed variants; never affected any economic or directional number in
this report.

### Conclusion

No variant meets the documented minimum evidence bar (§ below /
`NEXT_AGENT_TASKS.md`): sample sizes are 14–41 per baseline-family variant
(only `shadow_no_kelly` clears n≥300, and its incremental edge over baseline
is not distinguishable from zero), and this is a single continuous window,
not two independent out-of-sample weeks. **No strategy edge is established.
Live trading remains NOT APPROVED.**

### 14. Post-freeze tail — excluded, mixed-version, not part of Week 1

The `2026-07-20..27` runner was never stopped and kept running past the
`2026-07-26T16:23:41.723Z` freeze boundary while this session's three bug
fixes were written to disk. Every record with `timestamp` after that
boundary is a different code version than the frozen §10 dataset (proven via
`journalctl`: the first post-freeze child, PID 51642, already logs
`config.reconciliation=False`/`"Reconciliation deactivated"`, which only the
fixed `bot.py` produces). This span is preserved on disk but is the
**`post_freeze_unscoped_tail`** — explicitly excluded from every number in
§10, and never appended to the Week-1 dataset. See `PROJECT_STATE.md` for
the exact boundary and evidence. The recommended next step is an
independent, fixed-commit Week-2 window (§ `NEXT_AGENT_TASKS.md`), not an
extension of Week 1.
