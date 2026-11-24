"""Pluggable historical-price-source interface for the independent v2
reconstruction engine. Only OANDA practice is wired in -- see
HISTORICAL_DATA_SOURCE_ASSESSMENT.md for why the other candidates (Dukascopy,
HistData, Twelve Data) were assessed but not selected.

Every concrete source returns the same shape: a list of bar dicts with
pair/side/start_utc/end_utc/open/high/low/close/complete/evidence_id, exactly
the shape research_gap_evidence.candle_proof() already consumes -- this
engine reuses that function unmodified (see engine.py).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path


class HistoricalSource(ABC):
    """One bounded fetch per call: a single pair, a single [start, end) span.
    Implementations must never fabricate bars and must mark every bar's
    provenance (evidence_id) so it can be audited later."""

    @abstractmethod
    def fetch(self, pair: str, start: datetime, end: datetime) -> list[dict]:
        ...


class OandaPracticeSource(HistoricalSource):
    """GET-only, practice-environment-enforced, S5 bid/ask candles. Same
    bounded-request discipline as research_evidence_audit.py: at most one
    request per call, rounded to the S5 grid, capped at 4990 bars (24950s)
    per request so a caller can never accidentally ask for unbounded history
    in one shot. Responses are cached to disk, content-addressed by their
    exact request parameters, so a rerun against the same cache directory
    never re-fetches and is therefore deterministic without network access."""

    MAX_SPAN_SECONDS = 4990 * 5
    REQUEST_TIMEOUT_SECONDS = 8

    def __init__(self, cache_dir: Path, api_key: str | None = None, request_fn=None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._request_fn = request_fn
        self._api_key = api_key

    @staticmethod
    def _instrument(pair: str) -> str:
        return pair[:3] + "_" + pair[3:]

    def _cache_key(self, instrument: str, a: datetime, b: datetime) -> str:
        raw = json.dumps({"provider": "OANDA practice", "instrument": instrument,
                          "from": a.isoformat(), "to": b.isoformat(), "granularity": "S5", "price": "BA"},
                         sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def fetch(self, pair: str, start: datetime, end: datetime) -> list[dict]:
        if os.environ.get("OANDA_ENVIRONMENT") != "practice":
            raise RuntimeError("OandaPracticeSource requires OANDA_ENVIRONMENT=practice.")
        a = datetime.fromtimestamp(math.floor(start.timestamp() / 5) * 5, timezone.utc)
        b = datetime.fromtimestamp(math.ceil(end.timestamp() / 5) * 5, timezone.utc)
        if (b - a).total_seconds() > self.MAX_SPAN_SECONDS:
            raise ValueError(
                f"Requested span {(b - a).total_seconds():.0f}s exceeds the {self.MAX_SPAN_SECONDS}s "
                "per-request bound -- split into multiple calls rather than widening this one.")
        instrument = self._instrument(pair)
        key = self._cache_key(instrument, a, b)
        cache_path = self.cache_dir / f"{key}.json"
        if cache_path.exists():
            cached = json.loads(cache_path.read_text())
            response, fetched_at = cached["response"], cached["fetched_at_utc"]
        else:
            response = self._live_request(instrument, a, b)
            fetched_at = datetime.now(timezone.utc).isoformat()
            cache_path.write_text(json.dumps({"response": response, "fetched_at_utc": fetched_at}, sort_keys=True))
        return self._to_bars(pair, response, key, fetched_at)

    def _live_request(self, instrument: str, a: datetime, b: datetime) -> dict:
        params = {"from": a.isoformat(), "to": b.isoformat(), "price": "BA", "granularity": "S5"}
        if self._request_fn is not None:
            return self._request_fn(instrument, params)
        from oandapyV20 import API
        from oandapyV20.endpoints.instruments import InstrumentsCandles
        if not self._api_key:
            raise RuntimeError("OANDA practice credentials are not configured.")
        api = API(access_token=self._api_key, environment="practice",
                  request_params={"timeout": self.REQUEST_TIMEOUT_SECONDS})
        req = InstrumentsCandles(instrument=instrument, params=params)
        api.request(req)
        return req.response

    @staticmethod
    def _to_bars(pair: str, response: dict, evidence_id: str, fetched_at_utc: str) -> list[dict]:
        bars = []
        for c in response.get("candles", []):
            if c.get("complete") is not True:
                continue
            try:
                t = datetime.fromisoformat(c["time"])
            except (KeyError, ValueError, TypeError):
                continue
            if t.tzinfo is None:
                continue
            for side in ("bid", "ask"):
                px = c.get(side)
                if not px:
                    continue
                try:
                    o, h, l, cl = (float(px[k]) for k in ("o", "h", "l", "c"))
                except (KeyError, ValueError, TypeError):
                    continue
                if not all(math.isfinite(x) and x > 0 for x in (o, h, l, cl)):
                    continue
                bars.append({"pair": pair, "side": side, "complete": True,
                            "start_utc": t.isoformat(), "end_utc": (t + timedelta(seconds=5)).isoformat(),
                            "open": o, "high": h, "low": l, "close": cl,
                            "fetched_at_utc": fetched_at_utc, "evidence_sha256": evidence_id})
        return bars
