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
