"""
Telegram Outbound Notification Module

Sends alerts and summaries via Telegram Bot API (sendMessage).
Outbound-only — no polling, no webhook, no receive loop.

Requires environment variables (read from project .env):
  TELEGRAM_BOT_TOKEN — Telegram bot token
  TELEGRAM_CHAT_ID   — Target chat/user ID

Security:
  - Never prints or logs the bot token
  - Sanitizes all messages before sending
  - Masks wallet addresses in messages
  - Redacts secret values but preserves message context
"""
import os
import re
import json
import urllib.request
import urllib.error
import logging
from typing import Optional, Dict, Any

_log = logging.getLogger("telegram_notifier")


# ---------------------------------------------------------------------------
# Message sanitization — pattern-based, not keyword-line-based
# ---------------------------------------------------------------------------
_ADDRESS_RE = re.compile(r'0x[0-9a-fA-F]{10,}')
_TOKEN_LIKE_RE = re.compile(
    r'(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])'
)
_PRIVATE_KEY_RE = re.compile(
    r'(?:private[_\s]*key|priv[_\s]*key)\s*[:=]\s*\S+',
    re.IGNORECASE,
)
_API_KEY_RE = re.compile(
    r'(?:api[_\s]*key|api[_\s]*secret)\s*[:=]\s*\S+',
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r'(?:secret|password|token|bearer|auth)\s*[:=]\s*\S+',
    re.IGNORECASE,
)


def _mask_address(value: str) -> str:
    """Mask an Ethereum-like address: show first 6 and last 4 characters."""
    if not isinstance(value, str) or len(value) < 6:
        return "****"
    if len(value) < 14:
        return f"{value[:3]}...{value[-2:]}"
    return f"{value[:6]}...{value[-4:]}"


def _mask_secret(value: str) -> str:
    """Mask a secret value: show first 4 and last 2 characters."""
    if not isinstance(value, str) or len(value) < 6:
        return "[REDACTED]"
    return f"{value[:4]}...{value[-2:]}"


def _redact_kv(match: re.Match) -> str:
    """Redact the value portion of a key=value match."""
    text = match.group(0)
    for sep in [':', '=']:
        if sep in text:
            key, _, val = text.partition(sep)
            return f"{key}{sep}{_mask_secret(val.strip())}"
    return "[REDACTED]"


def _sanitize_message(text: str) -> str:
    """
    Sanitize a message before sending to Telegram.

    Strategy: redact specific values, NOT entire lines.
    - Wallet addresses (0x prefix, 10+ hex chars) -> masked
    - Long hex strings (32+ chars, likely tokens/keys) -> masked
    - secret=value patterns -> value masked
    - api_key=value patterns -> value masked
    - private_key=value patterns -> value masked

    Normal bot messages (trade alerts, lifecycle, errors) are preserved.
    """
    result = text
    result = _ADDRESS_RE.sub(lambda m: _mask_address(m.group()), result)
    result = _TOKEN_LIKE_RE.sub(lambda m: _mask_secret(m.group()), result)
    result = _PRIVATE_KEY_RE.sub(_redact_kv, result)
    result = _API_KEY_RE.sub(_redact_kv, result)
    result = _SECRET_RE.sub(_redact_kv, result)
    return result


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _load_telegram_config() -> Dict[str, Optional[str]]:
    """
    Load Telegram config from environment variables.
    Returns dict with 'token' and 'chat_id' (values may be None).
    Never returns the actual token value in logs.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    # Fallback: if chat_id is missing, check TELEGRAM_ALLOWED_USERS
    if not chat_id:
        allowed = os.getenv("TELEGRAM_ALLOWED_USERS", "").strip()
        if allowed:
            for part in allowed.split(","):
                part = part.strip()
                if part.isdigit():
                    chat_id = part
                    break

    return {
        "token": token if token else None,
        "chat_id": chat_id if chat_id else None,
    }


def _mask_chat_id(chat_id: Optional[str]) -> str:
    """Mask chat ID for logging."""
    if not chat_id or len(chat_id) < 4:
        return "****"
    return f"{chat_id[:2]}...{chat_id[-2:]}"


# ---------------------------------------------------------------------------
# Send functions
# ---------------------------------------------------------------------------
def send_telegram_message(text: str, dry_run: bool = False) -> bool:
    """
    Send a raw text message via Telegram Bot API.

    Args:
        text: Message text (will be sanitized before sending)
        dry_run: If True, log the message but don't actually send

    Returns:
        True if sent successfully, False otherwise
    """
    config = _load_telegram_config()

    if not config["token"]:
        _log.warning("Telegram: TELEGRAM_BOT_TOKEN not set — message not sent")
        return False

    if not config["chat_id"]:
        _log.warning("Telegram: TELEGRAM_CHAT_ID not set — message not sent")
        return False

    sanitized = _sanitize_message(text)

    if dry_run:
        _log.info(f"[DRY_RUN] Telegram message:\n{sanitized}")
        return True

    # Send via Telegram Bot API (outbound only, no polling)
    # Plain text mode (no parse_mode) to avoid Markdown parsing issues
    url = f"https://api.telegram.org/bot{config['token']}/sendMessage"
    payload = json.dumps({
        "chat_id": config["chat_id"],
        "text": sanitized,
        "disable_web_page_preview": True,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
            if body.get("ok"):
                msg_id = body.get("result", {}).get("message_id", "?")
                _log.info(f"Telegram: sent to {_mask_chat_id(config['chat_id'])} (msg_id={msg_id})")
                return True
            else:
                desc = body.get("description", "unknown error")
                _log.error(f"Telegram API error: {desc}")
                return False
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        _log.error(f"Telegram HTTP error: {e.code} — {body}")
        return False
    except Exception as e:
        _log.error(f"Telegram send failed: {e}")
        return False


def send_trade_alert(
    record: Dict[str, Any],
    dry_run: bool = False,
) -> bool:
    """
    Send a trade alert (simulated order or skipped trade).

    Args:
        record: Journal record dict with trade details
        dry_run: If True, log but don't send

    Returns:
        True if sent successfully
    """
    event_type = record.get("event_type", "unknown")

    if event_type == "simulated_order":
        emoji = "📊"
        title = "SIMULATED ORDER"
    elif event_type == "skipped_trade":
        emoji = "⏭"
        title = "SKIPPED TRADE"
    elif event_type == "order_rejected":
        emoji = "🚫"
        title = "ORDER REJECTED"
    else:
        emoji = "📋"
        title = event_type.upper()

    lines = [f"{emoji} {title}"]

    if record.get("dry_run") is not None:
        mode = "DRY_RUN" if record["dry_run"] else "LIVE"
        lines.append(f"Mode: {mode}")

    if record.get("side"):
        lines.append(f"Side: {record['side'].upper()}")

    if record.get("entry_price") is not None:
        lines.append(f"Entry: ${record['entry_price']:.4f}")

    if record.get("size") is not None:
        lines.append(f"Size: ${record['size']:.2f}")

    if record.get("p_stay") is not None:
        lines.append(f"Markov p_stay: {record['p_stay']:.4f}")

    if record.get("kelly_size") is not None:
        lines.append(f"Kelly size: ${record['kelly_size']:.2f}")

    if record.get("simulated_pnl") is not None:
        pnl = record["simulated_pnl"]
        pnl_emoji = "+" if pnl >= 0 else ""
        lines.append(f"P&L: {pnl_emoji}${pnl:.2f}")

    if record.get("decision_reason"):
        reason = record["decision_reason"][:100]
        lines.append(f"Reason: {reason}")

    text = "\n".join(lines)
    return send_telegram_message(text, dry_run=dry_run)


def send_daily_summary(
    summary: Dict[str, Any],
    dry_run: bool = False,
) -> bool:
    """
    Send daily journal summary via Telegram.

    Args:
        summary: Dict from journal.daily_summary()
        dry_run: If True, log but don't send

    Returns:
        True if sent successfully
    """
    date = summary.get("date", "unknown")
    lines = [
        f"📈 Daily Summary — {date}",
        "",
        f"Decisions: {summary.get('decisions', 0)}",
        f"Simulated orders: {summary.get('simulated_orders', 0)}",
        f"Skipped trades: {summary.get('skipped_trades', 0)}",
        f"Markov blocked: {summary.get('markov_blocked', 0)}",
        f"Kelly blocked: {summary.get('kelly_blocked', 0)}",
    ]

    avg_ps = summary.get("avg_p_stay")
    if avg_ps is not None:
        lines.append(f"Avg p_stay: {avg_ps:.4f}")
    avg_pm = summary.get("avg_p_model")
    if avg_pm is not None:
        lines.append(f"Avg p_model: {avg_pm:.4f}")
    avg_ks = summary.get("avg_kelly_size")
    if avg_ks is not None:
        lines.append(f"Avg Kelly size: ${avg_ks:.2f}")

    if summary.get("total_simulated_pnl") is not None:
        pnl = summary["total_simulated_pnl"]
        pnl_emoji = "+" if pnl >= 0 else ""
        lines.append(f"Simulated P&L: {pnl_emoji}${pnl:.2f}")

    total = summary.get("win_count", 0) + summary.get("loss_count", 0)
    if total > 0:
        wr = summary.get("win_count", 0) / total * 100
        lines.append(f"Win rate: {wr:.1f}% ({summary.get('win_count', 0)}/{total})")

    text = "\n".join(lines)
    return send_telegram_message(text, dry_run=dry_run)


def is_configured() -> bool:
    """Check if Telegram is configured (without printing values)."""
    config = _load_telegram_config()
    return config["token"] is not None and config["chat_id"] is not None


def get_status() -> Dict[str, Any]:
    """Get Telegram configuration status (no secrets)."""
    config = _load_telegram_config()
    return {
        "token_present": config["token"] is not None,
        "chat_id_present": config["chat_id"] is not None,
        "configured": config["token"] is not None and config["chat_id"] is not None,
        "chat_id_masked": _mask_chat_id(config["chat_id"]),
    }
