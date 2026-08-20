"""Thin client for Jase's existing Signal Engine (Project Signal) FastAPI app.

Signal Engine stays untouched and keeps running as its own service; this
module just reads published Signals for currency "Companies" and turns them
into PESTLE category scores per currency, per the design in
PESTLE_SIGNAL_ENGINE_INTEGRATION.md.

Runs in MOCK mode (bundled fixtures) until SIGNAL_ENGINE_BASE_URL is set and
the setup/ seed data has been loaded into a running Signal Engine instance.
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
import httpx

PESTLE_CATEGORIES = ["political", "economic", "social", "technological", "legal_regulatory", "environmental"]

# code -> (pestle category, polarity: +1 bullish for the currency / -1 bearish)
PESTLE_SIGNAL_POLARITY: dict[str, tuple[str, int]] = {
    "RATE_HIKE": ("economic", 1), "RATE_CUT": ("economic", -1),
    "CPI_ABOVE_FORECAST": ("economic", 1), "CPI_BELOW_FORECAST": ("economic", -1),
    "EMPLOYMENT_STRONG": ("economic", 1), "EMPLOYMENT_WEAK": ("economic", -1),
    "GDP_BEAT": ("economic", 1), "GDP_MISS": ("economic", -1),
    "TRADE_BALANCE_IMPROVE": ("economic", 1), "TRADE_BALANCE_WORSEN": ("economic", -1),
    "GOVERNMENT_STABILITY_POSITIVE": ("political", 1), "GOVERNMENT_STABILITY_NEGATIVE": ("political", -1),
    "SANCTIONS_TRADE_RESTRICTION": ("political", -1),
    "GEOPOLITICAL_CONFLICT_ESCALATION": ("political", -1), "GEOPOLITICAL_CONFLICT_DEESCALATION": ("political", 1),
    "CONSUMER_CONFIDENCE_UP": ("social", 1), "CONSUMER_CONFIDENCE_DOWN": ("social", -1),
    "LABOR_UNREST": ("social", -1),
    "FINANCIAL_INFRA_POSITIVE": ("technological", 1), "FINANCIAL_INFRA_NEGATIVE": ("technological", -1),
    "REGULATORY_CHANGE_POSITIVE": ("legal_regulatory", 1), "REGULATORY_CHANGE_NEGATIVE": ("legal_regulatory", -1),
    "CLIMATE_ENERGY_POLICY_POSITIVE": ("environmental", 1), "CLIMATE_ENERGY_POLICY_NEGATIVE": ("environmental", -1),
}

# Default category weights from MODEL_SPEC.md §4.1
DEFAULT_CATEGORY_WEIGHTS = {
    "economic": 0.40, "political": 0.20, "social": 0.10,
    "technological": 0.05, "legal_regulatory": 0.10, "environmental": 0.05,
}
COMMODITY_CURRENCIES = {"AUD", "CAD", "NZD"}
COMMODITY_CATEGORY_WEIGHTS = {**DEFAULT_CATEGORY_WEIGHTS, "environmental": 0.15, "economic": 0.30}

RECENCY_HALF_LIFE_HOURS = 36  # matches MODEL_SPEC.md §4.1 recency-weighting intent

MOCK_FIXTURE_PATH = Path(__file__).parent / "mock_signals.json"


@dataclass
class RawPestleSignal:
    currency: str
    signal_type_code: str
    observed_at: datetime
    confidence: float  # 0-1, from Signal Engine
    source_credibility: float  # 0-100, from Signal Engine
    description: str = ""  # the real evidence text, for narrative display


class SignalEngineClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = base_url or os.environ.get("SIGNAL_ENGINE_BASE_URL")
        self.mock = self.base_url is None

    def fetch_signals(self, currency: str, lookback_hours: int = 72) -> list[RawPestleSignal]:
        if self.mock:
            return self._fetch_mock(currency, lookback_hours)
        return self._fetch_live(currency, lookback_hours)

    def _fetch_live(self, currency: str, lookback_hours: int) -> list[RawPestleSignal]:
        with httpx.Client(base_url=self.base_url, timeout=10.0) as client:
            companies = client.get("/companies", params={"name": currency}).json()
            items = companies.get("items", companies) if isinstance(companies, dict) else companies
            if not items:
                return []
            company_id = items[0]["id"]
            timeline = client.get(f"/companies/{company_id}/timeline", params={"order": "newest", "event_type": "signal"}).json()
            out = []
            cutoff = datetime.now(timezone.utc).timestamp() - lookback_hours * 3600
            # Confirmed shape via curl against the live instance: a bare array of
            # rows with "timestamp" (naive, treated as UTC) and "signal_type_code" —
            # not "observed_at"/"items" as originally assumed.
            for row in timeline.get("items", timeline) if isinstance(timeline, dict) else timeline:
                observed = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                if observed.timestamp() < cutoff:
                    continue
                out.append(RawPestleSignal(
                    currency=currency,
                    signal_type_code=row["signal_type_code"],
                    observed_at=observed,
                    confidence=row.get("confidence", 0.7),
                    source_credibility=row.get("source_credibility", 70),
                    description=row.get("description", ""),
                ))
            return out

    def _fetch_mock(self, currency: str, lookback_hours: int) -> list[RawPestleSignal]:
        if not MOCK_FIXTURE_PATH.exists():
            return []
        data = json.loads(MOCK_FIXTURE_PATH.read_text())
        cutoff = datetime.now(timezone.utc).timestamp() - lookback_hours * 3600
        out = []
        for row in data.get(currency, []):
            observed = datetime.fromisoformat(row["observed_at"])
            if observed.timestamp() < cutoff:
                continue
            out.append(RawPestleSignal(
                currency=currency,
                signal_type_code=row["signal_type_code"],
                observed_at=observed,
                confidence=row.get("confidence", 0.7),
                source_credibility=row.get("source_credibility", 70),
                description=row.get("description", ""),
            ))
        return out
