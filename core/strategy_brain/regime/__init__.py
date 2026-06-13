"""
BTC Regime Diagnostics — Phase 1

Diagnostics-only layer. NONE of this affects trading decisions, gates,
Kelly sizing, Markov filtering, or risk limits.

Sub-modules:
  - daily_candles: BTC daily candle fetch + cache + macro trend regime
  - volatility:    realized volatility / range percentile regime
  - aggregator:    combine macro + intraday + volatility, persist snapshots

Phase 2 (NOT implemented here): crypto breadth / TradingView BPI-style index.
"""
