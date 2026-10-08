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
from data.oanda import to_oanda_instrument  # noqa: E402

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


def _real_request_fn():
    """Builds the REAL request function against OANDA, with an ACTUAL
    socket-level timeout applied via oandapyV20's own documented
    `request_params={"timeout": ...}` mechanism (API.__init__ merges this
    into every requests.Session call `request_args.update(self.
    _request_params)` inside API.request() -- confirmed by reading
    oandapyV20's own source, not assumed). Built fresh here, not via
    src/data/oanda.py's shared _client() (which v1 also depends on for its
    own hash-pinned integrity -- v2 must never touch that file). The
    client's own request_params["timeout"] is mutated before each call so
    a later attempt can use a SHORTER timeout as the remaining retry
    budget shrinks -- this is what actually bounds an in-flight socket
    call, not just the bookkeeping between attempts."""
    from oandapyV20 import API
    from oandapyV20.endpoints.pricing import PricingInfo
    api_key = os.environ.get("OANDA_API_KEY")
    if not api_key:
        raise RuntimeError("OANDA_API_KEY not set in environment — check setup/oanda.env is loaded")
    environment = os.environ.get("OANDA_ENVIRONMENT", "practice")
    client = API(access_token=api_key, environment=environment, request_params={"timeout": cfg.REQUEST_TIMEOUT_SECONDS})

    def request_fn(acct_id: str, instr: str, timeout_seconds: float):
        client._request_params["timeout"] = timeout_seconds  # mutated per-attempt -- see docstring above
        return client.request(PricingInfo(accountID=acct_id, params={"instruments": instr}))

    return request_fn


def fetch_pricing_samples(pairs: list[str], account_id: str | None = None, request_fn=None, now_fn=None,
                           sleep_fn=None, monotonic_fn=None) -> dict:
    """Returns {"samples": [...], "attempts": [...]} -- never raises for a
    retryable or auth failure (the caller gets an explicit empty-samples
    result plus the full attempt record instead); still raises for a
    genuine programming error (e.g. OANDA_ACCOUNT_ID missing), same as v1.

    Bounded retry: up to cfg.MAX_RETRY_ATTEMPTS additional attempts after
    the first, exponential backoff (cfg.RETRY_BACKOFF_BASE_SECONDS * 2**n),
    but the WHOLE call -- first attempt plus every retry -- is capped at
    cfg.MAX_TOTAL_REQUEST_SECONDS. That budget is tracked on a MONOTONIC
    clock (`monotonic_fn`, default time.monotonic) specifically so a
    wall-clock jump (NTP correction, system suspend/resume) can never
    mis-measure elapsed retry time -- `now_fn` (wall clock) is used only
    for the received_at_utc/started_at_utc LABELS attached to each
    attempt, never for duration arithmetic. Each real HTTP attempt is also
    given an actual socket-level timeout equal to whatever budget remains
    (never more than cfg.REQUEST_TIMEOUT_SECONDS), so a single hung
    request is bounded by BOTH the client's own timeout AND the loop's
    bookkeeping, not bookkeeping alone. An "auth" classified failure stops
    immediately, no retry, regardless of attempts remaining.

    `request_fn(account_id, instruments, timeout_seconds)`, `now_fn()`,
    `sleep_fn(seconds)` and `monotonic_fn()` are injected for testability;
    all default to the real OANDA call / real clocks / real sleep and are
    never overridden in production. See
    test_quote_client_v2_real_path.py for a test against this function
    with request_fn left at its real default, pointed at a genuinely slow
    LOCAL server -- not an injected fake that merely promises to honour a
    timeout."""
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    sleep_fn = sleep_fn or time.sleep
    monotonic_fn = monotonic_fn or time.monotonic
    account_id = account_id or os.environ.get("OANDA_ACCOUNT_ID")
    if not account_id:
        raise RuntimeError("OANDA_ACCOUNT_ID not set in environment — check setup/oanda.env is loaded")
    instruments = ",".join(to_oanda_instrument(p) for p in pairs)

    if request_fn is None:
        request_fn = _real_request_fn()

    attempts = []
    call_start_mono = monotonic_fn()
    for attempt_num in range(1, cfg.MAX_RETRY_ATTEMPTS + 2):  # first attempt + retries
        elapsed_before = monotonic_fn() - call_start_mono
        remaining_budget = cfg.MAX_TOTAL_REQUEST_SECONDS - elapsed_before
        if remaining_budget <= 0:
            attempts.append({"attempt": attempt_num, "skipped": True,
                             "reason": "max_total_request_seconds_exceeded", "elapsed_before": elapsed_before})
            break
        attempt_timeout = min(cfg.REQUEST_TIMEOUT_SECONDS, remaining_budget)
        attempt_started_at = now_fn()
        try:
            resp = request_fn(account_id, instruments, attempt_timeout)
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
            remaining_budget = cfg.MAX_TOTAL_REQUEST_SECONDS - (monotonic_fn() - call_start_mono)
            if attempt_num > cfg.MAX_RETRY_ATTEMPTS or remaining_budget <= 0:
                break
            backoff = cfg.RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt_num - 1))
            backoff = min(backoff, max(remaining_budget, 0))
            if backoff > 0:
                sleep_fn(backoff)

    return {"samples": [], "attempts": attempts}
