# PAPER EXECUTION MODEL

_DRY-RUN / DIAGNOSTIC. No live orders. Added in the NO-book-capture session
(2026-07-19)._

This document describes how the shadow harness turns a directional decision into
a **realistic, contemporaneous paper fill** using the actual observed order-book
depth of the correct outcome token — and why the historical SHORT economics were
invalid before this work.

## 1. Why historical SHORT P/L was invalid

The bot subscribed **only** to the YES/Up outcome instrument. For a SHORT
(BUY NO) decision there was no NO quote at all, so any economic figure would have
required either:

- reusing the YES midpoint, or
- computing `1 - yes_midpoint` as the NO price.

Both are wrong: the NO book has its own bid/ask, spread, and depth, and
`1 - yes_mid` is a **directional identity**, not an **executable** price. The
harness therefore (correctly) marked every SHORT `economic_pnl_unavailable`
with reason `no_token_ask_not_captured` rather than fabricating a number.

## 2. Why NO must use the observed NO ask

To BUY the NO token you pay the **NO ask** (top of the NO book), consuming real
resting size. The executable entry for a SHORT is therefore
`selected_token_id == no_token_id` and `selected_exec_price` = a contemporaneous
NO ask. The harness **never** substitutes the YES midpoint, `1 - yes_mid`, or a
closed-market quote. The `1 - yes_mid` value is still recorded, but **only** as a
labelled diagnostic cross-check (`no_mid_source="derived_from_yes_mid"`).

## 3. Order-book subscription architecture

- `core/market_data/dual_book.py` — `DualBookStore` keyed **by token id**
  (never by market). It holds a `TokenBook` for the YES and NO tokens with
  best bid/ask, sizes, quote timestamp, source, subscription generation, and a
  validity flag.
- `bot.py` subscribes to **both** the YES and NO instruments
  (`_activate_dual_books()` derives the NO token id, computes the condition id,
  and calls `subscribe_quote_ticks` on the NO instrument). `on_quote_tick`
  routes each tick to the matching token book by instrument/token id.
- Snapshots and incremental top-of-book updates are applied per token
  (`apply_snapshot` / `apply_top_of_book`). Ticks for an unknown token id are
  rejected (no cross-market contamination).
- On disconnect the generation is bumped and both books are invalidated
  (`on_disconnect`); a fresh snapshot after `on_resubscribe` restores
  executability. Pre-reconnect data can never be treated as live.
- `set_market(...)` bumps the generation and clears both books when the token
  ids change, so an expired market is replaced safely.

## 4. Freshness and skew rules (configurable)

Thresholds live in `strategy.yaml` → `market_data:` (mirrored in
`config.py DEFAULTS`):

| Key | Default | Meaning |
|-----|---------|---------|
| `quote_freshness_seconds` | `60` | A book older than this is `stale` and **not** executable. |
| `cross_book_skew_warn_seconds` | `5` | YES vs NO quote-timestamp gap above this raises a non-blocking flag. |
| `max_spread_warn` | `0.10` | Per-token bid/ask spread above this raises a non-blocking flag. |
| `sum_bid_max` | `1.05` | `yes_bid + no_bid` above this raises a flag. |
| `sum_ask_min` | `0.95` | `yes_ask + no_ask` below this raises a flag. |
| `paper_fixed_risk` | `1.00` | Fixed max-risk stake per paper fill (USD). |

A book is executable only when its status is `ok` (present, fresh, not crossed,
prices in `(0,1)`, current subscription generation). Otherwise the executable
ask is `None` → `economic_pnl_unavailable`.

## 5. Paper fill / VWAP semantics

`core/strategy_brain/paper_execution.py::simulate_fill` is a **pure** function,
separate from the strategy. Given the observed ask ladder of the selected token
and a fixed requested stake it walks the ladder consuming `price * size` until
the stake budget is exhausted, and records:

- requested stake / requested shares, top-of-book ask and its size,
- the ask levels actually consumed, filled shares, spent, and **VWAP** fill
  price, unfilled stake,
- `fill_status`: `filled`, `partially_filled`, or `unavailable`,
- slippage model (`walk_observed_ask_depth`), fee source/rate, gross/net P/L,
  max loss, and return on capital at risk.

It **never fabricates liquidity**: if the observed depth is smaller than the
requested stake the result is a labelled partial fill (or `unavailable` under
the `unavailable` policy) — never a silent unlimited-liquidity fill at the best
ask.

## 6. Liquidity limitations

`nautilus_trader` quote ticks expose only **top-of-book** (a single level) per
instrument. Live paper fills therefore walk a one-level ladder unless a
full-depth REST snapshot is supplied (as the smoke test does via
`/book`). Multi-level VWAP is fully supported and unit-tested; live depth beyond
L1 is only as deep as the snapshot source provides.

## 7. Fee semantics

The harness inspects whether a fee rate is available. If none is exposed it
labels the record `pre_fee` (`fee_source="pre_fee"`, `fee_rate=None`) and does
**not** invent a fee. When a rate is supplied it is applied and its source and
timestamp are recorded.

## 8. Executable vs directional (four separate quantities)

The record keeps these strictly separate and never conflates them:

1. **Signal / model probability** — the strategy's estimate.
2. **Market midpoint** — directional reference only.
3. **Best executable ask** of the selected token — the entry you would pay.
4. **Paper VWAP fill price** — depth-aware simulated fill.

Official settlement outcome is a fifth, independent quantity used only for P/L
resolution.

## 9. Smoke-test proof

`scripts/no_book_smoke_test.py` (read-only, bounded, ≤600s) discovers the
current BTC 15m market, subscribes to both books, and proves a **real NO ask**
is obtainable. A live run captured `no_ask` with both books `ok` and all
validation flags healthy — closing the capability gap that made SHORT
economics unavailable.
