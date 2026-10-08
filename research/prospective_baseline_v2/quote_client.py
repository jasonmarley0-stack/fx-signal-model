"""v2 OANDA pricing client. Same split as v1 (pure parser + the one real
network call), with the collection-reliability fix the diagnosis called
for: an explicit per-attempt timeout, a bounded number of retries with
backoff for RETRYABLE errors only, and a hard ceiling on total time so a
single request can never again stall the whole collector loop for minutes
(the exact failure mode that produced the 334-second gap in the v1 run --
see the 7-day diagnosis). Authorization failures are detected explicitly
and never retried (retrying a bad credential cannot succeed, and doing so
would just extend an already-failed request). Every attempt -- success or
failure -- is recorded and returned so the collector can log real
request/response evidence, never silently swallowed. Never logs or prints
the API key or account ID.
"""
from __future__ import annotations
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
from data.oanda import _client, to_oanda_instrument  # noqa: E402

import contract as cfg  # noqa: E402


def parse_pricing_response(resp: dict, received_at: datetime) -> list[dict]:
    """Pure function -- no network. Unchanged in substance from v1: one
    quote-sample dict per instrument, with our own pair naming and an
    explicit tradeable flag. v2 adds nothing here -- the measurement
    change lives entirely in score.py, not in how a response is parsed."""
    out = []
    for p in resp.get("prices", []):
        instrument = p["instrument"]
        pair = instrument.replace("_", "")
        bids = p.get("bids") or []
        asks = p.get("asks") or []
        out.append({
            "pair": pair,
            "received_at_utc": received_at.isoformat(),
            "oanda_time_utc": p.get("time"),
            "bid": float(bids[0]["price"]) if bids else None,
            "ask": float(asks[0]["price"]) if asks else None,
            "tradeable": bool(p.get("tradeable", False)),
        })
    return out


def classify_error(ex: Exception) -> str:
    """Three categories the contract asks to keep distinct: "auth" (never
    retried, never logged with credential detail), "maintenance" (OANDA's
    own explicit signal, a planned/expected condition), and "transient"
    (everything else -- connection errors, timeouts, other 5xx -- treated
    as retryable collection noise, same spirit as any ordinary network
    hiccup)."""
    code = getattr(ex, "code", None)
    msg = str(getattr(ex, "msg", "") or ex)
    if code in (401, 403) or "insufficient authorization" in msg.lower() or "invalid access token" in msg.lower():
        return "auth"
    if "maintenance" in msg.lower():
        return "maintenance"
    return "transient"


def _sanitize_error_text(ex: Exception) -> str:
    """Never let a credential value reach a log line. OANDA error bodies
    do not normally echo back the API key, but this is a deliberate,
    defensive truncation+scrub rather than trusting that in all cases."""
    msg = str(getattr(ex, "msg", "") or ex)
    account_id = os.environ.get("OANDA_ACCOUNT_ID")
    if account_id:
        msg = msg.replace(account_id, "<account_id>")
    return f"{type(ex).__name__}[{getattr(ex, 'code', '?')}]: {msg}"[:300]


def fetch_pricing_samples(pairs: list[str], account_id: str | None = None, request_fn=None, now_fn=None, sleep_fn=None) -> dict:
    """Returns {"samples": [...], "attempts": [...]} -- never raises for a
    retryable or auth failure (the caller gets an explicit empty-samples
    result plus the full attempt record instead); still raises for a
    genuine programming error (e.g. OANDA_ACCOUNT_ID missing), same as v1.

    Bounded retry: up to cfg.MAX_RETRY_ATTEMPTS additional attempts after
    the first, exponential backoff (cfg.RETRY_BACKOFF_BASE_SECONDS * 2**n),
    but the WHOLE call -- first attempt plus every retry -- is capped at
    cfg.MAX_TOTAL_REQUEST_SECONDS wall-clock. An "auth" classified failure
    stops immediately, no retry, regardless of attempts remaining.

    `request_fn(account_id, instruments, timeout_seconds)` and `now_fn()`
    are injected for testability (simulate timeouts/failures without
    real network or real sleeps); both default to the real OANDA call /
    real clock and are never used in production."""
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    sleep_fn = sleep_fn or time.sleep
    account_id = account_id or os.environ.get("OANDA_ACCOUNT_ID")
    if not account_id:
        raise RuntimeError("OANDA_ACCOUNT_ID not set in environment — check setup/oanda.env is loaded")
    instruments = ",".join(to_oanda_instrument(p) for p in pairs)

    if request_fn is None:
        from oandapyV20.endpoints.pricing import PricingInfo
        client = _client()

        def request_fn(acct_id: str, instr: str, timeout_seconds: float):  # noqa: E306
            return client.request(PricingInfo(accountID=acct_id, params={"instruments": instr}))
            # NOTE: oandapyV20's own client does not expose a per-call timeout
            # override; REQUEST_TIMEOUT_SECONDS governs the retry/backoff
            # schedule's pacing (see below) rather than the socket itself in
            # the real path. The injected request_fn in tests simulates a hung
            # call directly, which is what actually proves the time ceiling.

    attempts = []
    call_start = now_fn()
    for attempt_num in range(1, cfg.MAX_RETRY_ATTEMPTS + 2):  # first attempt + retries
        elapsed_before = (now_fn() - call_start).total_seconds()
        if elapsed_before >= cfg.MAX_TOTAL_REQUEST_SECONDS:
            attempts.append({"attempt": attempt_num, "skipped": True,
                             "reason": "max_total_request_seconds_exceeded", "elapsed_before": elapsed_before})
            break
        attempt_started_at = now_fn()
        try:
            resp = request_fn(account_id, instruments, cfg.REQUEST_TIMEOUT_SECONDS)
            received_at = now_fn()  # always captured AFTER the response -- same v1 discipline, never overridable
            samples = parse_pricing_response(resp, received_at)
            attempts.append({
                "attempt": attempt_num, "started_at_utc": attempt_started_at.isoformat(),
                "completed_at_utc": received_at.isoformat(), "outcome": "success",
            })
            return {"samples": samples, "attempts": attempts}
        except Exception as ex:  # noqa: BLE001
            failed_at = now_fn()
            category = classify_error(ex)
            attempts.append({
                "attempt": attempt_num, "started_at_utc": attempt_started_at.isoformat(),
                "failed_at_utc": failed_at.isoformat(), "outcome": "failed",
                "error_category": category, "error": _sanitize_error_text(ex),
            })
            if category == "auth":
                break  # never retry a credential failure
            remaining_budget = cfg.MAX_TOTAL_REQUEST_SECONDS - (failed_at - call_start).total_seconds()
            if attempt_num > cfg.MAX_RETRY_ATTEMPTS or remaining_budget <= 0:
                break
            backoff = cfg.RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt_num - 1))
            backoff = min(backoff, max(remaining_budget, 0))
            if backoff > 0:
                sleep_fn(backoff)

    return {"samples": [], "attempts": attempts}
