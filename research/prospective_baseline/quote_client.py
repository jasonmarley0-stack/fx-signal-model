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


def fetch_pricing_samples(pairs: list[str], received_at: datetime | None = None) -> list[dict]:
    """The one function that actually calls OANDA. `received_at` defaults
    to real wall-clock time -- the LOCAL receipt time, recorded separately
    from OANDA's own `time` field on each price (see parse_pricing_response),
    since the two can differ (network/processing delay) and this system
    tracks that distinction rather than conflating them."""
    from oandapyV20.endpoints.pricing import PricingInfo
    account_id = os.environ.get("OANDA_ACCOUNT_ID")
    if not account_id:
        raise RuntimeError("OANDA_ACCOUNT_ID not set in environment — check setup/oanda.env is loaded")
    received_at = received_at or datetime.now(timezone.utc)
    client = _client()
    instruments = ",".join(to_oanda_instrument(p) for p in pairs)
    resp = client.request(PricingInfo(accountID=account_id, params={"instruments": instruments}))
    return parse_pricing_response(resp, received_at)
