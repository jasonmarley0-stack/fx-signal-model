"""Turns raw Signal Engine evidence into PESTLE scores per currency and per pair.

See MODEL_SPEC.md §4 and PESTLE_SIGNAL_ENGINE_INTEGRATION.md for the design.
"""
from __future__ import annotations
import math
from datetime import datetime, timezone
from .signal_engine_client import (
    SignalEngineClient, PESTLE_SIGNAL_POLARITY, DEFAULT_CATEGORY_WEIGHTS,
    COMMODITY_CATEGORY_WEIGHTS, COMMODITY_CURRENCIES, RECENCY_HALF_LIFE_HOURS,
    PESTLE_CATEGORIES,
)

# Signal Engine's timeline dates each signal by when the real-world event
# happened (source_published_at), not by when it was reviewed/published —
# correct (a rate decision should be dated when it happened, not when a
# human got around to approving it), but it means evidence review lag eats
# directly into whatever window we score against. Evidence review here is
# deliberately manual/considered (see SIGNAL_ENGINE_SETUP.md — no
# auto-classification), so it will never be same-day for every item; 72h
# left almost no runway once review happened even a few days after
# publication (discovered 2026-08-24: a batch reviewed 3-4 days after
# publication came back with pestle_score still 0.0 everywhere). Widened to
# 5 days to tolerate a realistic review cadence — RECENCY_HALF_LIFE_HOURS
# (36h) still decays older evidence's weight sharply within that window, so
# this doesn't make week-old news carry full weight, just usable weight.
DEFAULT_LOOKBACK_HOURS = 120


def _recency_weight(observed_at: datetime, half_life_hours: float = RECENCY_HALF_LIFE_HOURS) -> float:
    age_hours = (datetime.now(timezone.utc) - observed_at).total_seconds() / 3600
    return math.pow(0.5, max(age_hours, 0) / half_life_hours)


def category_scores(currency: str, client: SignalEngineClient | None = None,
                     lookback_hours: int = DEFAULT_LOOKBACK_HOURS) -> dict[str, float]:
    """Returns {category: score in [-1, 1]} for one currency."""
    client = client or SignalEngineClient()
    signals = client.fetch_signals(currency, lookback_hours=lookback_hours)

    bucket_pos = {c: 0.0 for c in PESTLE_CATEGORIES}
    bucket_neg = {c: 0.0 for c in PESTLE_CATEGORIES}

    for sig in signals:
        polarity_entry = PESTLE_SIGNAL_POLARITY.get(sig.signal_type_code)
        if polarity_entry is None:
            continue
        category, polarity = polarity_entry
        weight = sig.confidence * (sig.source_credibility / 100) * _recency_weight(sig.observed_at)
        if polarity > 0:
            bucket_pos[category] += weight
        else:
            bucket_neg[category] += weight

    scores = {}
    for cat in PESTLE_CATEGORIES:
        total = bucket_pos[cat] + bucket_neg[cat]
        scores[cat] = 0.0 if total == 0 else max(-1.0, min(1.0, (bucket_pos[cat] - bucket_neg[cat]) / max(total, 1.0)))
    return scores


def currency_pestle_score(currency: str, client: SignalEngineClient | None = None,
                           lookback_hours: int = DEFAULT_LOOKBACK_HOURS) -> tuple[float, dict[str, float]]:
    """Returns (weighted composite score in [-1,1], per-category breakdown)."""
    cats = category_scores(currency, client=client, lookback_hours=lookback_hours)
    weights = COMMODITY_CATEGORY_WEIGHTS if currency in COMMODITY_CURRENCIES else DEFAULT_CATEGORY_WEIGHTS
    composite = sum(cats[c] * weights[c] for c in PESTLE_CATEGORIES)
    return max(-1.0, min(1.0, composite)), cats


def pair_pestle_score(pair: str, client: SignalEngineClient | None = None,
                       lookback_hours: int = DEFAULT_LOOKBACK_HOURS) -> dict:
    """pair like 'GBPUSD' -> dict with pair score + both currencies' breakdowns."""
    base, quote = pair[:3], pair[3:]
    base_score, base_cats = currency_pestle_score(base, client=client, lookback_hours=lookback_hours)
    quote_score, quote_cats = currency_pestle_score(quote, client=client, lookback_hours=lookback_hours)
    pair_score = max(-1.0, min(1.0, base_score - quote_score))
    return {
        "pair": pair,
        "pestle_score": pair_score,
        "base": {"currency": base, "score": base_score, "categories": base_cats},
        "quote": {"currency": quote, "score": quote_score, "categories": quote_cats},
    }
