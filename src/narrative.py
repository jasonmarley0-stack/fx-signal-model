"""Turns raw scores back into plain-English explanations for the dashboard —
"why does this pair look the way it does", quoting the real evidence behind
the PESTLE score and naming which technical components fired.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pestle.signal_engine_client import PESTLE_SIGNAL_POLARITY

_TECH_LABELS = {
    "orb": "Opening range breakout",
    "trend": "Trend/momentum (EMA+MACD)",
    "pattern": "Candlestick pattern",
}


def technical_narrative(tech_row: dict) -> str:
    """tech_row like {'orb': 1, 'trend': 0, 'pattern': -1, 'composite': ...}."""
    active = []
    for key, label in _TECH_LABELS.items():
        val = tech_row.get(key, 0)
        if val > 0:
            active.append(f"{label} bullish")
        elif val < 0:
            active.append(f"{label} bearish")
    if not active:
        return "No technical components fired on the latest bar — composite score is driven by drift/noise only."
    return "Fired: " + "; ".join(active) + "."


def _age_str(observed_at: datetime) -> str:
    hours = (datetime.now(timezone.utc) - observed_at).total_seconds() / 3600
    if hours < 1:
        return "<1h ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


def pestle_narrative(currency: str, raw_signals: list, max_items: int = 3) -> str:
    """raw_signals: list[RawPestleSignal] for this currency, as returned by
    SignalEngineClient.fetch_signals — used for the real evidence text, not
    just the aggregated score."""
    if not raw_signals:
        return f"No recent {currency} PESTLE evidence in the lookback window — score is flat by default, not a null result."
    ranked = sorted(raw_signals, key=lambda s: s.observed_at, reverse=True)[:max_items]
    lines = []
    for sig in ranked:
        polarity_entry = PESTLE_SIGNAL_POLARITY.get(sig.signal_type_code)
        direction = ""
        if polarity_entry:
            direction = "bullish" if polarity_entry[1] > 0 else "bearish"
        text = sig.description or sig.signal_type_code
        lines.append(f"[{_age_str(sig.observed_at)}, {direction or 'n/a'}] {text}")
    return " / ".join(lines)
