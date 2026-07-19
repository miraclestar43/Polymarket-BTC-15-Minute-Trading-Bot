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
- Regression tests lock in every fix (252 tests pass).

## 7. Recommendation

Do **not** treat any historical economic P/L as evidence of edge. Only
directional accuracy is established. A forward DRY-RUN with schema v2 is required
to gather **executable** NO-side quotes before any economic conclusion — and
before any consideration of live trading.
