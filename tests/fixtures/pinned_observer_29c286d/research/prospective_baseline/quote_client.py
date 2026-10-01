"""Thin OANDA pricing client, split into a pure parsing function (tested
without network/credentials) and a real network call (used only by
observe.py's live loop, never by tests). Reuses src/data/oanda.py's
authenticated client (same OANDA_API_KEY/OANDA_ENVIRONMENT env handling)
rather than re-implementing auth -- OANDA_ACCOUNT_ID is additionally
required here (PricingInfo, unlike InstrumentsCandles, needs an account).
Never logs or prints either credential.
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
from data.oanda import _client, to_oanda_instrument  # noqa: E402


def parse_pricing_response(resp: dict, received_at: datetime) -> list[dict]:
    """Pure function -- no network. `resp` is OANDA PricingInfo's raw JSON
    (a `prices` list, one entry per requested instrument). Returns one
    quote-sample dict per instrument, with our own pair naming
    (to_oanda_instrument's inverse) and an explicit tradeable flag --
    OANDA's own signal for whether this instrument can currently be
    dealt, distinct from whether a bid/ask happens to be present."""
    out = []
    for p in resp.get("prices", []):
        instrument = p["instrument"]  # e.g. "EUR_USD"
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


def fetch_pricing_samples(pairs: list[str], account_id: str | None = None, request_fn=None, now_fn=None) -> list[dict]:
    """The one function that actually calls OANDA. There is NO parameter
    that lets a caller override the post-response receipt time -- that
    was the production defect (observe.py's run_quote_tick threaded its
    own pre-request clock reading into this function, silently
    reinstating the "stamped before the request completes" bug this
    function otherwise fixes). Receipt time is always this function's
    OWN clock reading, taken AFTER the HTTP response returns, never
    before the request is sent and never supplied by the caller. The
    request-start time is preserved separately (request_started_at_utc,
    returned on each sample) for diagnosing slow requests only.

    `request_fn(account_id, instruments) -> raw OANDA response dict` and
    `now_fn() -> datetime` are injected for testability (a test can
    simulate a slow/delayed response without real network or a real
    sleep); both default to the real OANDA call / real clock and are
    never used in production."""
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    account_id = account_id or os.environ.get("OANDA_ACCOUNT_ID")
    if not account_id:
        raise RuntimeError("OANDA_ACCOUNT_ID not set in environment — check setup/oanda.env is loaded")
    instruments = ",".join(to_oanda_instrument(p) for p in pairs)
    if request_fn is None:
        from oandapyV20.endpoints.pricing import PricingInfo
        client = _client()

        def request_fn(acct_id: str, instr: str):  # noqa: E306
            return client.request(PricingInfo(accountID=acct_id, params={"instruments": instr}))

    request_started_at = now_fn()
    resp = request_fn(account_id, instruments)
    received_at = now_fn()  # always captured AFTER the request returns -- never overridable
    samples = parse_pricing_response(resp, received_at)
    for s in samples:
        s["request_started_at_utc"] = request_started_at.isoformat()
    return samples
