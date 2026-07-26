# DATA SCHEMA

_The corrected, self-describing data model. DRY-RUN research data only._

## The core lesson (why the old data misled us)

A single field, `entry_price`, silently meant different things:

- In shadow records it was **always the YES-token midpoint** `(bid+ask)/2`,
  regardless of side.
- In settlement/economics it was consumed as **the selected token's cost**.

For SHORT (BUY NO) trades those are *not the same number*: the NO cost is
`1 − YES_price`. Using the YES midpoint as the NO cost fabricated payouts.

**Rule going forward: never let a field named `entry_price` mean different
things across schemas, and never backfill a historical entry price from a closed
market's current CLOB state.**

## Four independent concept groups (record separately)

| Group      | Meaning                              | Fields (examples)                              |
|------------|--------------------------------------|------------------------------------------------|
| Identity   | *Which* market / token              | `condition_id`, `yes_token_id`, `no_token_id`, `selected_token_id` |
| Timing     | *When* things happened              | `decision_ts`, `market_start_time`, `market_end`, `quote_timestamp` |
| Signal     | *What the model thought*            | model probability, `shadow_direction`, edge (never an "entry price") |
| Economics  | *What it would actually cost/pay*   | `yes_bid/ask/mid`, `no_bid/ask/mid`, `selected_exec_price`         |

Model probability is a **signal**, never an executable price.

## Shadow schema v2 — `executable_quote` block

Emitted by `core/strategy_brain/shadow_evaluator._forward_schema_fields`;
`SHADOW_SCHEMA_VERSION = 2`.

| field                              | meaning |
|------------------------------------|---------|
| `schema_version`                   | `2` |
| `condition_id`                     | market condition id (identity) |
| `market_start_time`                | 15-min window start epoch |
| `yes_token_id` / `no_token_id`     | both outcome token ids |
| `selected_token_id`                | token actually taken (YES for long, NO for short) |
| `yes_bid` / `yes_ask` / `yes_mid`  | subscribed YES book |
| `no_bid` / `no_ask` / `no_mid`     | NO book (from book if available) |
| `no_mid_source`                    | `no_book` \| `derived_from_yes_mid` (NOT executable) \| `unavailable` |
| `quote_timestamp` / `quote_source` | when/where the quote came from |
| `selected_exec_price`              | executable ask for the selected token, or `null` |
| `selected_exec_price_source`       | how the exec price was obtained |
| `economic_pnl_unavailable`         | `true` when no valid executable selected-token quote exists |
| `economic_pnl_unavailable_reason`  | why (e.g. `executable_quote_unavailable`) |

When `economic_pnl_unavailable` is `true`, downstream reporting **must not**
compute recorded economic P/L for that record. It reports directional accuracy
and, separately, a clearly-labelled synthetic (midpoint) sensitivity number.

## Pricing convention (fixed at source)

`core/strategy_brain/economics.selected_token_price(side, yes_price)`:

- long  → `yes_price`
- short → `1 − yes_price`  (validates `0 < yes_price < 1`)

`settle_trade` / `compute_pnl` derive the exec price by side unless an explicit
`selected_exec_price` is supplied, so every caller is corrected automatically.

## Historical recoverability

Historical records stored **only midpoints** and **no NO-token quote or quote
timestamp**. Therefore **historical executable economic P/L is not recoverable**
and is reported as *unavailable* — never guessed or substituted with model
probability.

## Official resolution store (`logs/official_resolutions.jsonl`)

Per-record: `status`, `resolved_outcome` (UP/DOWN), `condition_id`,
`requested_condition_id`, `market_slug`, `winning_token_id`, and (parser
`clob-resolution-2`) `losing_token_id`, `winning_price`, `tokens_summary`,
`parser_version`. Trust requires `requested==returned` condition id, a single
winner, and an Up/Down label — `price==1` alone is insufficient.

## Schema-v2 completeness (2026-07-19)

The self-describing `executable_quote` block now also carries two fields that
were previously left null but are derivable from data already in the record
(no network call, no strategy impact):

- `condition_id` — from `market_resolution.condition_id`, else parsed from the
  instrument id (`"0x"+64hex` before the `-` token-id separator).
- `market_end_time` — `market_start_time + 900` (15-minute market).

Executable-price capture by side (verified live):

- **LONG (BUY YES):** `selected_token_id = yes_token_id`,
  `selected_exec_price = yes_ask`, `selected_exec_price_source = "yes_ask"`,
  `economic_pnl_unavailable = false`.
- **SHORT (BUY NO):** NO book not subscribed →
  `selected_exec_price = null`, `economic_pnl_unavailable = true`
  (`economic_pnl_unavailable_reason = "no_token_ask_not_captured"`). The YES
  price is never substituted for the NO cost.

## Schema-v2 full economic record (dual-book capture, 2026-07-19)

With both outcome books subscribed (`DualBookStore`, keyed by token id), the
`executable_quote` block is extended **additively** — no old field is redefined.
A v1 reader still finds every original field; v2 readers gain the following.
See [PAPER_EXECUTION_MODEL.md](PAPER_EXECUTION_MODEL.md) for the semantics.

**Per-token quotes & sizes**

| field | meaning |
|-------|---------|
| `yes_bid_size` / `yes_ask_size` | top-of-book YES sizes |
| `no_bid_size` / `no_ask_size` | top-of-book NO sizes |
| `yes_observed_bid/ask` / `no_observed_bid/ask` | last observed quote (recorded even when not executable) |
| `yes_ask_ladder` / `no_ask_ladder` | observed ask ladder `[(price,size),…]` |
| `selected_token_ask_size` | size at the selected token's top ask |

**Timing / integrity**

| field | meaning |
|-------|---------|
| `yes_quote_timestamp` / `no_quote_timestamp` | per-token quote capture epoch |
| `yes_quote_age_s` / `no_quote_age_s` | quote age at snapshot |
| `selected_quote_age_s` | age of the selected token's quote |
| `cross_book_skew_s` | \|YES ts − NO ts\| |
| `yes_book_status` / `no_book_status` | `ok`/`missing`/`stale`/`crossed`/`out_of_range`/`expired` |
| `book_generation` | subscription/reconnect generation |
| `validation_flags` | non-blocking booleans (True = healthy): `yes_bid_le_ask`, `no_bid_le_ask`, `prices_in_range`, `tokens_distinct`, `cross_book_skew_ok`, `yes_spread_ok`, `no_spread_ok`, `sum_bid_not_above_1`, `sum_ask_not_below_1`, `selected_token_matches_side`, `quote_before_or_at_decision` |

**Paper execution (VWAP fill)**

| field | meaning |
|-------|---------|
| `paper_requested_stake` / `paper_requested_shares` | fixed-risk stake and implied shares |
| `paper_fill_status` | `filled` / `partially_filled` / `unavailable` |
| `paper_filled_shares` / `paper_spent` / `paper_vwap` | depth-aware simulated fill |
| `paper_unfilled_stake` | stake not filled from observed depth |
| `paper_top_ask` / `paper_top_ask_size` / `paper_ask_levels_used` | ladder actually consumed |
| `paper_slippage_model` | `walk_observed_ask_depth` |
| `paper_fee_rate` / `paper_fee_source` / `paper_pre_fee` | fee assumptions (`pre_fee` when none exposed — never invented) |
| `executable_economic_available` | true only when the selected book is `ok` and a fill was produced |

`market_id_v2` carries the market id under a new key so the original `market_id`
is never clobbered.

## `would_trade` / `shadow_side` semantics (updated 2026-07-26)

`would_trade=true` requires `shadow_side` to be `"long"` or `"short"` (a
`has_valid_side` gate in `core/strategy_brain/shadow_evaluator.py`). Records
written **before** 2026-07-26 by the four Kelly-relaxed variants
(`shadow_no_kelly`, `shadow_allow_kelly_minus_002`, `shadow_allow_kelly_minus_005`,
`relaxed_markov_and_kelly_shadow`) may still show `would_trade=true` with
`shadow_side="unknown"` and `selected_token_id=null` — a proven forward
labeling bug (fixed forward only; historical records were not rewritten).
Any consumer of `shadow_decisions_*.jsonl` **must** filter on
`shadow_side in ("long", "short")` before treating a row as a real trade
candidate, exactly as `scripts/dual_book_run_audit.py` already does. The new
skip-reason value `no_valid_side: actual_direction=unknown` marks a correctly
labeled no-trade going forward.

## Run provenance fields (added 2026-07-26)

Every shadow record now carries a run-provenance block, computed **once per
run session** by `15m_bot_runner.py` (`core/diagnostics/run_provenance.py`)
and read back by each child from `POLY_RUN_*` environment variables — never
a Git call or file hash per record, never per-child either (it is the same
value for every child of the same runner session):

| field | meaning |
|-------|---------|
| `run_id` | `run_<runner_start_epoch>_<runner_pid>` — stable for the whole run session |
| `git_head` | full 40-char commit SHA the runner started from (local `git rev-parse HEAD`, no fetch) |
| `git_branch` | branch name at runner start |
| `git_dirty` | `true`/`false`/`"unavailable"` — whether the working tree had uncommitted changes at runner start |
| `strategy_config_sha256` | SHA256 of `strategy.yaml` at runner start |

`"unavailable"` (not a crash) is used whenever Git or a config file could
not be read. See `logs/runs/<run_id>/run_manifest.json` /
`run_manifest_end.json` for the fuller run-level record (Python version,
hostname, `requirements.txt` hash, `dry_run`/`live_trading_ack`, lifecycle
counts) — no secrets or raw environment contents are ever written to either
the manifest or the shadow records.
