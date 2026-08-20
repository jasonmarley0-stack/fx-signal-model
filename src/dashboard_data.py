"""Builds the data payload the morning dashboard renders: for each configured
pair, the technical score (from the latest bars), the PESTLE score/breakdown,
the combined signal, and SL/TP.
"""
from __future__ import annotations
import pandas as pd
from strategies.composite import technical_score
from pestle.pestle_scorer import pair_pestle_score
from pestle.signal_engine_client import SignalEngineClient
from combiner import combine_signal
from narrative import technical_narrative, pestle_narrative
from calendar_events import upcoming_events_for_pair

DEFAULT_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]


def build_dashboard_payload(pair_dataframes: dict[str, pd.DataFrame], pairs: list[str] | None = None) -> dict:
    """pair_dataframes: {pair: recent OHLC DataFrame (30-min bars, tz-aware UTC index)}"""
    pairs = pairs or DEFAULT_PAIRS
    client = SignalEngineClient()
    rows = []
    for pair in pairs:
        df = pair_dataframes.get(pair)
        if df is None or len(df) < 60:
            continue
        tech = technical_score(df)
        latest_tech = tech.iloc[-1]
        pestle = pair_pestle_score(pair, client=client)
        entry = df["close"].iloc[-1]
        latest_bar_time = df.index[-1]
        signal = combine_signal(
            pair=pair, entry=entry, atr_value=latest_tech["atr"],
            tech_score=latest_tech["tech_score"], pestle_score=pestle["pestle_score"],
            generated_at=latest_bar_time.to_pydatetime(),
        )
        tech_dict = {"orb": latest_tech["orb"], "trend": latest_tech["trend"],
                     "pattern": latest_tech["pattern"], "composite": latest_tech["tech_score"]}
        base, quote = pair[:3], pair[3:]
        base_signals = client.fetch_signals(base, lookback_hours=72)
        quote_signals = client.fetch_signals(quote, lookback_hours=72)
        upcoming = upcoming_events_for_pair(client, pair, within_hours=24 * 7)
        rows.append({
            "pair": pair,
            "entry": entry,
            "tech": tech_dict,
            "pestle": pestle,
            "signal": signal,
            "narrative": {
                "technical": technical_narrative(tech_dict),
                "pestle_base": pestle_narrative(base, base_signals),
                "pestle_quote": pestle_narrative(quote, quote_signals),
            },
            "upcoming": [
                {"currency": e.currency, "scheduled_at_utc": e.scheduled_at.isoformat(), "title": e.title}
                for e in upcoming
            ],
        })
    return {"generated_at": pd.Timestamp.utcnow().isoformat(), "rows": rows}
