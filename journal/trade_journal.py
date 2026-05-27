"""
Trade Journal — Persistent Decision & Trade Logging

Records every trading decision, simulated order, skipped trade,
and rejected order in JSONL format (one JSON object per line).

Files are rotated daily:
  logs/trade_journal_YYYY-MM-DD.jsonl

No secrets (private keys, API keys, tokens, wallet addresses)
are ever written to the journal.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field, asdict
from loguru import logger

from config import get_config


# ---------------------------------------------------------------------------
# Sensitive fields that must NEVER be logged
# ---------------------------------------------------------------------------
_SENSITIVE_KEYS = frozenset({
    "private_key", "api_key", "api_secret", "api_passphrase",
    "polymarket_pk", "telegram_bot_token", "telegram_chat_id",
    "safe_address", "funder", "polymarket_funder",
    "password", "secret", "auth", "bearer",
})

_SENSITIVE_PATTERNS = ("0x",)  # Ethereum address prefixes


def _is_sensitive(key: str, value: Any) -> bool:
    """Check if a key-value pair should be redacted."""
    key_lower = key.lower()
    if key_lower in _SENSITIVE_KEYS:
        return True
    if any(pat in key_lower for pat in ("key", "secret", "token", "password", "auth")):
        return True
    return False


def _sanitize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Remove or redact sensitive fields from a journal record."""
    sanitized = {}
    for k, v in record.items():
        if _is_sensitive(k, v):
            sanitized[k] = "[REDACTED]"
        elif isinstance(v, str) and len(v) > 20:
            # Check if it looks like an address or key
            if any(v.startswith(pat) for pat in _SENSITIVE_PATTERNS):
                sanitized[k] = v[:6] + "…" + v[-4:] if len(v) > 10 else "[REDACTED]"
            else:
                sanitized[k] = v
        else:
            sanitized[k] = v
    return sanitized


# ---------------------------------------------------------------------------
# Journal record
# ---------------------------------------------------------------------------
@dataclass
class JournalRecord:
    """A single journal entry."""
    timestamp: str
    event_type: str           # decision | simulated_order | skipped_trade | order_rejected
    dry_run: bool
    market_slug: Optional[str] = None
    condition_id: Optional[str] = None
    token_id: Optional[str] = None
    side: Optional[str] = None
    entry_price: Optional[float] = None
    size: Optional[float] = None
    p_model: Optional[float] = None
    p_model_source: Optional[str] = None
    odds_b: Optional[float] = None
    raw_kelly_fraction: Optional[float] = None
    capped_kelly_fraction: Optional[float] = None
    kelly_size: Optional[float] = None
    markov_state: Optional[str] = None
    p_stay: Optional[float] = None
    min_prob: Optional[float] = None
    edge: Optional[float] = None
    fee_estimate: Optional[float] = None
    decision_reason: Optional[str] = None
    order_id: Optional[str] = None
    simulated_pnl: Optional[float] = None
    mark_price: Optional[float] = None
    error_type: Optional[str] = None
    error_message_sanitized: Optional[str] = None


# ---------------------------------------------------------------------------
# Journal writer
# ---------------------------------------------------------------------------
class TradeJournal:
    """
    Persistent trade journal using JSONL format.

    Each day gets its own file: logs/trade_journal_YYYY-MM-DD.jsonl
    """

    def __init__(self, base_path: Optional[str] = None):
        """
        Initialize journal.

        Args:
            base_path: Base path for journal files (without date suffix).
                       If None, reads from strategy.yaml config.
        """
        if base_path is None:
            config = get_config()
            base_path = config.get("journal_path", "logs/trade_journal")

        self._base_path = base_path
        self._today = None
        self._file = None
        self._records_today = 0

        # Ensure directory exists
        Path(base_path).parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Trade journal initialized: {base_path}_YYYY-MM-DD.jsonl")

    def _get_today_path(self) -> str:
        """Get today's journal file path."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return f"{self._base_path}_{today}.jsonl"

    def _ensure_file(self) -> None:
        """Ensure the current day's file is open."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._today != today:
            # Day changed or first write — close old file, open new
            if self._file:
                try:
                    self._file.close()
                except Exception:
                    pass
            path = self._get_today_path()
            self._file = open(path, "a", encoding="utf-8")
            self._today = today
            self._records_today = 0

    def record(self, event_type: str, **kwargs) -> None:
        """
        Write a journal record.

        Args:
            event_type: One of: decision, simulated_order, skipped_trade, order_rejected
            **kwargs: Non-sensitive fields to record
        """
        self._ensure_file()

        # Always include these fields
        record = JournalRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=event_type,
            dry_run=kwargs.pop("dry_run", True),
            **kwargs,
        )

        # Convert to dict and sanitize
        data = asdict(record)
        # Remove None values to keep journal clean
        data = {k: v for k, v in data.items() if v is not None}
        data = _sanitize_record(data)

        try:
            line = json.dumps(data, default=str) + "\n"
            self._file.write(line)
            self._file.flush()
            self._records_today += 1
        except Exception as e:
            logger.error(f"Failed to write journal record: {e}")

    def log_decision(
        self,
        dry_run: bool,
        market_slug: Optional[str] = None,
        side: Optional[str] = None,
        entry_price: Optional[float] = None,
        p_model: Optional[float] = None,
        p_model_source: Optional[str] = None,
        odds_b: Optional[float] = None,
        raw_kelly_fraction: Optional[float] = None,
        capped_kelly_fraction: Optional[float] = None,
        kelly_size: Optional[float] = None,
        markov_state: Optional[str] = None,
        p_stay: Optional[float] = None,
        min_prob: Optional[float] = None,
        edge: Optional[float] = None,
        decision_reason: Optional[str] = None,
        **extra,
    ) -> None:
        """Log a trading decision."""
        self.record(
            "decision",
            dry_run=dry_run,
            market_slug=market_slug,
            side=side,
            entry_price=entry_price,
            p_model=p_model,
            p_model_source=p_model_source,
            odds_b=odds_b,
            raw_kelly_fraction=raw_kelly_fraction,
            capped_kelly_fraction=capped_kelly_fraction,
            kelly_size=kelly_size,
            markov_state=markov_state,
            p_stay=p_stay,
            min_prob=min_prob,
            edge=edge,
            decision_reason=decision_reason,
            **extra,
        )

    def log_simulated_order(
        self,
        dry_run: bool,
        market_slug: Optional[str] = None,
        side: Optional[str] = None,
        entry_price: Optional[float] = None,
        size: Optional[float] = None,
        order_id: Optional[str] = None,
        simulated_pnl: Optional[float] = None,
        mark_price: Optional[float] = None,
        **extra,
    ) -> None:
        """Log a simulated (dry-run) order."""
        self.record(
            "simulated_order",
            dry_run=dry_run,
            market_slug=market_slug,
            side=side,
            entry_price=entry_price,
            size=size,
            order_id=order_id,
            simulated_pnl=simulated_pnl,
            mark_price=mark_price,
            **extra,
        )

    def log_skipped_trade(
        self,
        dry_run: bool,
        decision_reason: str,
        market_slug: Optional[str] = None,
        side: Optional[str] = None,
        entry_price: Optional[float] = None,
        p_model: Optional[float] = None,
        markov_state: Optional[str] = None,
        p_stay: Optional[float] = None,
        **extra,
    ) -> None:
        """Log a skipped trade."""
        self.record(
            "skipped_trade",
            dry_run=dry_run,
            decision_reason=decision_reason,
            market_slug=market_slug,
            side=side,
            entry_price=entry_price,
            p_model=p_model,
            markov_state=markov_state,
            p_stay=p_stay,
            **extra,
        )

    def log_order_rejected(
        self,
        dry_run: bool,
        decision_reason: str,
        market_slug: Optional[str] = None,
        side: Optional[str] = None,
        error_type: Optional[str] = None,
        error_message_sanitized: Optional[str] = None,
        **extra,
    ) -> None:
        """Log a rejected order."""
        self.record(
            "order_rejected",
            dry_run=dry_run,
            decision_reason=decision_reason,
            market_slug=market_slug,
            side=side,
            error_type=error_type,
            error_message_sanitized=error_message_sanitized,
            **extra,
        )

    def close(self) -> None:
        """Close the journal file."""
        if self._file:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    @property
    def records_today(self) -> int:
        return self._records_today


# ---------------------------------------------------------------------------
# Daily summary helper
# ---------------------------------------------------------------------------
def daily_summary(date_str: Optional[str] = None) -> Dict[str, Any]:
    """
    Summarize today's (or given date's) journal.

    Args:
        date_str: Date in YYYY-MM-DD format. Defaults to today (UTC).

    Returns:
        Summary dict with counts and aggregates.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    config = get_config()
    base_path = config.get("journal_path", "logs/trade_journal")
    file_path = f"{base_path}_{date_str}.jsonl"

    summary = {
        "date": date_str,
        "total_records": 0,
        "decisions": 0,
        "simulated_orders": 0,
        "skipped_trades": 0,
        "rejected_orders": 0,
        "markov_blocked": 0,
        "kelly_blocked": 0,
        "avg_p_stay": None,
        "avg_p_model": None,
        "avg_kelly_size": None,
        "total_simulated_pnl": None,
        "win_count": 0,
        "loss_count": 0,
    }

    if not os.path.exists(file_path):
        return summary

    p_stay_values = []
    p_model_values = []
    kelly_sizes = []
    pnl_values = []

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                summary["total_records"] += 1
                event = record.get("event_type", "")

                if event == "decision":
                    summary["decisions"] += 1
                elif event == "simulated_order":
                    summary["simulated_orders"] += 1
                    pnl = record.get("simulated_pnl")
                    if pnl is not None:
                        pnl_values.append(pnl)
                        if pnl > 0:
                            summary["win_count"] += 1
                        elif pnl < 0:
                            summary["loss_count"] += 1
                elif event == "skipped_trade":
                    summary["skipped_trades"] += 1
                    reason = record.get("decision_reason", "")
                    if "markov" in reason.lower():
                        summary["markov_blocked"] += 1
                    elif "kelly" in reason.lower() or "negative_kelly" in reason.lower():
                        summary["kelly_blocked"] += 1
                elif event == "order_rejected":
                    summary["rejected_orders"] += 1

                # Collect numeric fields for averages
                ps = record.get("p_stay")
                if ps is not None:
                    p_stay_values.append(ps)
                pm = record.get("p_model")
                if pm is not None:
                    p_model_values.append(pm)
                ks = record.get("kelly_size")
                if ks is not None:
                    kelly_sizes.append(ks)

    except Exception as e:
        logger.error(f"Error reading journal for summary: {e}")

    # Compute averages
    if p_stay_values:
        summary["avg_p_stay"] = round(sum(p_stay_values) / len(p_stay_values), 4)
    if p_model_values:
        summary["avg_p_model"] = round(sum(p_model_values) / len(p_model_values), 4)
    if kelly_sizes:
        summary["avg_kelly_size"] = round(sum(kelly_sizes) / len(kelly_sizes), 2)
    if pnl_values:
        summary["total_simulated_pnl"] = round(sum(pnl_values), 2)

    return summary


def print_daily_summary(date_str: Optional[str] = None) -> None:
    """Print today's journal summary to stdout."""
    summary = daily_summary(date_str)
    print("=" * 60)
    print(f"Trade Journal Summary — {summary['date']}")
    print("=" * 60)
    print(f"  Total records:     {summary['total_records']}")
    print(f"  Decisions:         {summary['decisions']}")
    print(f"  Simulated orders:  {summary['simulated_orders']}")
    print(f"  Skipped trades:    {summary['skipped_trades']}")
    print(f"  Rejected orders:   {summary['rejected_orders']}")
    print(f"  Markov blocked:    {summary['markov_blocked']}")
    print(f"  Kelly blocked:     {summary['kelly_blocked']}")
    if summary["avg_p_stay"] is not None:
        print(f"  Avg p_stay:        {summary['avg_p_stay']:.4f}")
    if summary["avg_p_model"] is not None:
        print(f"  Avg p_model:       {summary['avg_p_model']:.4f}")
    if summary["avg_kelly_size"] is not None:
        print(f"  Avg Kelly size:    ${summary['avg_kelly_size']:.2f}")
    if summary["total_simulated_pnl"] is not None:
        print(f"  Simulated P&L:     ${summary['total_simulated_pnl']:+.2f}")
        total = summary["win_count"] + summary["loss_count"]
        if total > 0:
            wr = summary["win_count"] / total * 100
            print(f"  Win rate:          {wr:.1f}% ({summary['win_count']}/{total})")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_journal_instance: Optional[TradeJournal] = None


def get_journal() -> TradeJournal:
    """Get or create the singleton journal."""
    global _journal_instance
    if _journal_instance is None:
        _journal_instance = TradeJournal()
    return _journal_instance
