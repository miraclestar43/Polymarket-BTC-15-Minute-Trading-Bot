"""
Official Polymarket resolution retrieval — DRY-RUN / DIAGNOSTIC ONLY.

Why this module exists
----------------------
The prior backfill produced 0 official resolutions. Investigation showed the
root cause was in ``market_resolver._fetch_market``: it queried Gamma with
``/markets?condition_id=<id>`` (SINGULAR). Gamma silently IGNORES that
parameter, returns an unfiltered default list, and the resolver took
``data[0]`` — an arbitrary, unrelated market (e.g. a Rihanna-album market).
Those wrong markets were never "resolved", so everything was recorded as
unavailable/pending. Additionally, Gamma does NOT index the ephemeral 15-min
BTC markets at all (``/markets?condition_ids=<id>`` returns 0 rows).

The reliable source is the CLOB market endpoint:

    GET https://clob.polymarket.com/markets/{condition_id}

which returns the closed market with a ``tokens`` array; the winning token has
``winner: true`` (and ``price: 1``) and an ``outcome`` of "Up"/"Down" (or
"Yes"/"No"). This module retrieves and PARSES that safely, validates that the
returned market's condition id matches what was requested (so a wrong market is
never trusted), records the request identifier + parsing status, distinguishes
failure modes, and supports a fully offline fixture mode for tests.

It NEVER fabricates an outcome and NEVER places or influences an order.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.strategy_brain.market_resolver import (
    OUTCOME_OFFICIAL,
    OUTCOME_UNAVAILABLE,
)

CLOB_BASE = "https://clob.polymarket.com"
OFFICIAL_STORE_FILE = "official_resolutions.jsonl"

# Bumped whenever the parse logic changes so stored records are traceable.
PARSER_VERSION = "clob-resolution-2"

# Parsing / retrieval status taxonomy (kept explicit for auditing).
STATUS_RESOLVED = "resolved"
STATUS_PENDING = "pending"
STATUS_NOT_FOUND = "not_found"
STATUS_MALFORMED = "malformed"
STATUS_NETWORK_FAILURE = "network_failure"
STATUS_IDENTIFIER_UNAVAILABLE = "identifier_unavailable"

_OUTCOME_MAP = {
    "UP": "UP", "YES": "UP",
    "DOWN": "DOWN", "NO": "DOWN",
}


def _norm_outcome(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return _OUTCOME_MAP.get(str(raw).strip().upper())


def parse_clob_market(
    data: Any, requested_condition_id: str
) -> Dict[str, Any]:
    """
    Parse a CLOB ``/markets/{condition_id}`` payload into an official-resolution
    record. PURE (no network) so it is fully unit-testable.

    Validation:
      * payload must be a dict,
      * its condition_id MUST equal ``requested_condition_id`` (guards against
        the historical wrong-market bug),
      * a resolved market is ``closed == True`` with exactly one winning token.

    Returns a dict with: status, resolved_outcome, resolution_source,
    condition_id, requested_condition_id, question, market_slug,
    winning_token_id, parse_error.
    """
    out: Dict[str, Any] = {
        "status": STATUS_MALFORMED,
        "resolved_outcome": None,
        "resolution_source": OUTCOME_UNAVAILABLE,
        "condition_id": None,
        "requested_condition_id": requested_condition_id,
        "question": None,
        "market_slug": None,
        "winning_token_id": None,
        "losing_token_id": None,
        "winning_price": None,
        "winning_outcome_raw": None,
        "tokens_summary": None,
        "parser_version": PARSER_VERSION,
        "parse_error": None,
    }

    if not isinstance(data, dict):
        out["parse_error"] = "payload_not_object"
        return out

    returned_cid = data.get("condition_id") or data.get("conditionId")
    out["condition_id"] = returned_cid
    out["question"] = data.get("question")
    out["market_slug"] = data.get("market_slug") or data.get("slug")

    if not returned_cid:
        out["parse_error"] = "missing_condition_id_in_response"
        return out
    if str(returned_cid).lower() != str(requested_condition_id).lower():
        out["status"] = STATUS_NOT_FOUND
        out["parse_error"] = "condition_id_mismatch"
        return out

    tokens = data.get("tokens")
    if not isinstance(tokens, list) or not tokens:
        out["parse_error"] = "missing_tokens"
        return out

    # Bounded, auditable summary of every token (identity of both sides).
    tokens_summary = [
        {
            "token_id": t.get("token_id"),
            "outcome": t.get("outcome"),
            "winner": t.get("winner"),
            "price": t.get("price"),
        }
        for t in tokens if isinstance(t, dict)
    ]
    out["tokens_summary"] = tokens_summary

    closed = bool(data.get("closed", False))

    # Prefer the explicit winner flag. Only fall back to price==1 when NO token
    # carries a winner flag, and even then require an unambiguous single match
    # with a recognizable outcome (price==1 alone is not sufficient identity).
    flagged = [t for t in tokens if isinstance(t, dict) and t.get("winner") is True]
    priced = [t for t in tokens if isinstance(t, dict) and t.get("price") in (1, 1.0)]
    if flagged:
        winners = flagged
    else:
        winners = priced

    if not closed:
        out["status"] = STATUS_PENDING
        out["parse_error"] = "market_not_closed"
        return out

    if len(winners) != 1:
        # closed but zero or ambiguous winners -> do NOT fabricate an outcome.
        out["status"] = STATUS_PENDING if not winners else STATUS_MALFORMED
        out["parse_error"] = (
            "closed_no_winner" if not winners else "closed_multiple_winners")
        return out

    win_tok = winners[0]
    outcome = _norm_outcome(win_tok.get("outcome"))
    if outcome is None:
        # A winner with no recognizable Up/Down/Yes/No label is ambiguous
        # identity — reject rather than trust a bare price==1.
        out["parse_error"] = f"unrecognized_outcome:{win_tok.get('outcome')!r}"
        return out

    win_token_id = win_tok.get("token_id")
    losers = [t.get("token_id") for t in tokens
              if isinstance(t, dict) and t.get("token_id") != win_token_id]

    out["status"] = STATUS_RESOLVED
    out["resolved_outcome"] = outcome
    out["resolution_source"] = OUTCOME_OFFICIAL
    out["winning_token_id"] = win_token_id
    out["losing_token_id"] = losers[0] if len(losers) == 1 else None
    out["winning_price"] = win_tok.get("price")
    out["winning_outcome_raw"] = win_tok.get("outcome")
    out["parse_error"] = None
    return out


# Opener signature: (url, timeout) -> (http_status:int, parsed_json:Any)
Opener = Callable[[str, float], Any]


def _default_opener(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "PolymarketBot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_official(
    condition_id: Optional[str],
    *,
    opener: Optional[Opener] = None,
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """
    Retrieve + parse the official resolution for one condition id via CLOB.

    ``opener`` is injectable so tests (and the offline fixture mode) never touch
    the network. Distinguishes not_found / pending / malformed / network_failure
    / identifier_unavailable and records the request identifier.
    """
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not condition_id:
        return {
            "status": STATUS_IDENTIFIER_UNAVAILABLE,
            "resolved_outcome": None,
            "resolution_source": OUTCOME_UNAVAILABLE,
            "condition_id": None,
            "requested_condition_id": condition_id,
            "request_url": None,
            "retrieved_at": now_iso,
            "parse_error": "no_condition_id",
        }

    url = f"{CLOB_BASE}/markets/{condition_id}"
    op = opener or _default_opener
    try:
        data = op(url, timeout)
    except urllib.error.HTTPError as e:
        status = STATUS_NOT_FOUND if e.code == 404 else STATUS_NETWORK_FAILURE
        return {
            "status": status, "resolved_outcome": None,
            "resolution_source": OUTCOME_UNAVAILABLE,
            "condition_id": None, "requested_condition_id": condition_id,
            "request_url": url, "retrieved_at": now_iso,
            "parse_error": f"http_{e.code}",
        }
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {
            "status": STATUS_NETWORK_FAILURE, "resolved_outcome": None,
            "resolution_source": OUTCOME_UNAVAILABLE,
            "condition_id": None, "requested_condition_id": condition_id,
            "request_url": url, "retrieved_at": now_iso,
            "parse_error": f"network:{type(e).__name__}",
        }
    except (json.JSONDecodeError, ValueError):
        return {
            "status": STATUS_MALFORMED, "resolved_outcome": None,
            "resolution_source": OUTCOME_UNAVAILABLE,
            "condition_id": None, "requested_condition_id": condition_id,
            "request_url": url, "retrieved_at": now_iso,
            "parse_error": "json_decode_error",
        }

    parsed = parse_clob_market(data, condition_id)
    parsed["request_url"] = url
    parsed["retrieved_at"] = now_iso
    return parsed


# ---------------------------------------------------------------------------
# Offline fixture mode (deterministic, no network)
# ---------------------------------------------------------------------------
def make_fixture_opener(fixtures: Dict[str, Any]) -> Opener:
    """
    Build an opener that serves ``fixtures`` keyed by condition id (the last
    path segment of the URL). A fixture value may be a payload dict, or the
    string ``"__http_404__"`` / ``"__network__"`` to simulate failures.
    """
    def _opener(url: str, timeout: float) -> Any:
        cid = url.rstrip("/").split("/")[-1]
        if cid not in fixtures:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        val = fixtures[cid]
        if val == "__http_404__":
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        if val == "__network__":
            raise urllib.error.URLError("simulated network failure")
        if val == "__malformed__":
            raise json.JSONDecodeError("bad", "", 0)
        return val
    return _opener


# ---------------------------------------------------------------------------
# Clean official store (separate from the polluted market_resolutions_cache)
# ---------------------------------------------------------------------------
def load_official_store(log_dir: str = "logs") -> Dict[str, Dict[str, Any]]:
    """Load the clean official-resolution store keyed by condition id."""
    path = Path(log_dir) / OFFICIAL_STORE_FILE
    out: Dict[str, Dict[str, Any]] = {}
    if not path.exists():
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = rec.get("requested_condition_id") or rec.get("condition_id")
            if cid:
                out[str(cid).lower()] = rec
    return out


def official_outcome_for(
    condition_id: Optional[str], store: Dict[str, Dict[str, Any]]
) -> Optional[str]:
    """Return a validated official UP/DOWN outcome from the clean store, or None."""
    if not condition_id:
        return None
    rec = store.get(str(condition_id).lower())
    if not rec or rec.get("status") != STATUS_RESOLVED:
        return None
    outcome = rec.get("resolved_outcome")
    return str(outcome).upper() if outcome else None
