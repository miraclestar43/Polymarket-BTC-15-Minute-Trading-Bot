# PROJECT STATE

_Last updated: 2026-07-19 (forensic-audit session). DRY-RUN ONLY._

## What this project is

A shadow/paper research harness for a BTC 15-minute Up/Down strategy on
Polymarket. It records model decisions and would-be trades but **does not place
live orders** while `DRY_RUN=true` / `LIVE_TRADING_ACK=false`.

## Current safety posture

- `DRY_RUN=true` (default in `config.py DEFAULTS`).
- `LIVE_TRADING_ACK=false` (only ever read from `.env`, never from YAML).
- `polymarket-dryrun.service` is **inactive/disabled** — untouched this session.
- No live order path is enabled.

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

`.venv/bin/python -m pytest tests/ -q` → **252 passed**.

## Open risks / not done

- Forward economic P/L will only become valid once **live executable NO-side
  quotes** are captured during a real run (schema v2 is ready; needs a run).
- The on-disk `official_resolutions.jsonl` store was built by the **old parser**
  (no `losing_token_id`/`tokens_summary`); re-backfilling needs network and is
  intentionally deferred (no live calls this session).

See `RESEARCH_FINDINGS.md` for the full audit and `DATA_SCHEMA.md` for the
field-by-field data model.
