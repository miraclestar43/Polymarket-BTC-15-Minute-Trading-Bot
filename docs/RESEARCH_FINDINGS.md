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
