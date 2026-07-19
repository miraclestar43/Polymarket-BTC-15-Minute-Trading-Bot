"""
Read-only CLOB / Gamma market-data client (DRY-RUN / DIAGNOSTIC ONLY).

Fetches BOTH outcome-token order books for a BTC 15-minute market over plain
HTTP GET. Used by the connectivity smoke test to PROVE that a real,
contemporaneous NO-token ask can be captured. It performs read-only requests
only: it never authenticates a trade, signs, submits, or places an order, and
carries no credentials.

Injectable openers keep every unit test fully offline/deterministic.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
SLUG_PREFIX = "btc-updown-15m-"
MARKET_INTERVAL_SECONDS = 900

Ladder = List[Tuple[float, float]]
# Opener signature: (url, timeout) -> parsed_json
Opener = Callable[[str, float], Any]


def current_btc_15m_slug(now: Optional[float] = None) -> str:
    """Slug of the BTC 15-minute market whose interval contains ``now``."""
    ts = int(now if now is not None else time.time())
    start = ts - (ts % MARKET_INTERVAL_SECONDS)
    return f"{SLUG_PREFIX}{start}"


def _default_opener(url: str, timeout: float) -> Any:
    req = urllib.request.Request(
        url, headers={"User-Agent": "PolymarketBot-ReadOnly/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _parse_levels(raw: Any) -> Ladder:
    out: Ladder = []
    for lvl in raw or []:
        try:
            if isinstance(lvl, dict):
                price, size = float(lvl["price"]), float(lvl["size"])
            else:
                price, size = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        out.append((price, size))
    return out


def discover_current_market(
    now: Optional[float] = None,
    *,
    opener: Optional[Opener] = None,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """
    Discover the current BTC 15-minute market via Gamma and map outcome labels
    to token ids. Returns condition_id, yes_token_id (Up), no_token_id (Down),
    slug and question. Raises no exception on a missing market — returns a dict
    with ``error`` set instead.
    """
    slug = current_btc_15m_slug(now)
    op = opener or _default_opener
    url = f"{GAMMA_BASE}/markets?slug={slug}"
    try:
        data = op(url, timeout)
    except Exception as e:  # noqa: BLE001 - reported, never raised to caller
        return {"error": f"gamma_fetch_failed:{type(e).__name__}", "slug": slug,
                "request_url": url}

    markets = data if isinstance(data, list) else data.get("data") or []
    if not markets:
        return {"error": "market_not_found", "slug": slug, "request_url": url}
    m = markets[0]

    try:
        token_ids = m.get("clobTokenIds")
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
    except (json.JSONDecodeError, TypeError):
        return {"error": "malformed_token_ids", "slug": slug, "request_url": url}

    yes_token_id = no_token_id = None
    for tok, label in zip(token_ids or [], outcomes or []):
        up = str(label).strip().upper()
        if up in ("UP", "YES"):
            yes_token_id = str(tok)
        elif up in ("DOWN", "NO"):
            no_token_id = str(tok)
    # Fallback: positional (index 0 = Up/Yes, index 1 = Down/No).
    if (yes_token_id is None or no_token_id is None) and token_ids and len(token_ids) >= 2:
        yes_token_id = yes_token_id or str(token_ids[0])
        no_token_id = no_token_id or str(token_ids[1])

    return {
        "slug": slug,
        "condition_id": m.get("conditionId") or m.get("condition_id"),
        "question": m.get("question"),
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        "closed": bool(m.get("closed")),
        "request_url": url,
    }


def fetch_book(
    token_id: str,
    *,
    opener: Optional[Opener] = None,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """
    Fetch one token's order book (read-only) via ``GET /book?token_id=``.
    Returns bids/asks ladders plus the server timestamp when present.
    """
    op = opener or _default_opener
    url = f"{CLOB_BASE}/book?token_id={token_id}"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        data = op(url, timeout)
    except urllib.error.HTTPError as e:
        return {"error": f"http_{e.code}", "token_id": token_id,
                "request_url": url, "retrieved_at": now_iso}
    except Exception as e:  # noqa: BLE001
        return {"error": f"fetch_failed:{type(e).__name__}", "token_id": token_id,
                "request_url": url, "retrieved_at": now_iso}

    bids = _parse_levels(data.get("bids") if isinstance(data, dict) else None)
    asks = _parse_levels(data.get("asks") if isinstance(data, dict) else None)
    server_ts = None
    if isinstance(data, dict) and data.get("timestamp"):
        try:
            server_ts = float(data["timestamp"]) / 1000.0  # ms epoch -> s
        except (TypeError, ValueError):
            server_ts = None
    return {
        "token_id": token_id,
        "bids": bids,
        "asks": asks,
        "server_timestamp": server_ts,
        "retrieved_at": now_iso,
        "request_url": url,
    }


def make_fixture_opener(fixtures: Dict[str, Any]) -> Opener:
    """
    Offline opener for tests. Keys are matched as substrings of the URL (e.g.
    ``"slug=btc-updown-15m-1000"`` or ``"token_id=NO1"``). A value of the string
    ``"__error__"`` simulates a network failure.
    """
    def _opener(url: str, timeout: float) -> Any:
        for key, val in fixtures.items():
            if key in url:
                if val == "__error__":
                    raise urllib.error.URLError("simulated failure")
                return val
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
    return _opener
