"""Combines ORB + trend + pattern strategies into one technical score in [-1, 1].

See MODEL_SPEC.md §3.4.
"""
from __future__ import annotations
import pandas as pd
from .indicators import atr
from .opening_range_breakout import orb_signal, SESSION_OPENS_UTC
from .trend_following import trend_signal
from .candlestick_patterns import pattern_signal

DEFAULT_WEIGHTS = {"orb": 0.5, "trend": 0.3, "pattern": 0.2}


def technical_score(df: pd.DataFrame, session_open: str = SESSION_OPENS_UTC["london"],
                     weights: dict | None = None) -> pd.DataFrame:
    """Returns a DataFrame with component scores + combined tech_score column."""
    weights = weights or DEFAULT_WEIGHTS
    a = atr(df)

    df = df.copy()
    df["date"] = df.index.date
    daily = df.groupby("date").agg(day_high=("high", "max"), day_low=("low", "min"))
    prior_day_high = df["date"].map(daily["day_high"].shift(1))
    prior_day_low = df["date"].map(daily["day_low"].shift(1))
    df.drop(columns=["date"], inplace=True)

    orb = orb_signal(df, session_open=session_open)
    trend = trend_signal(df)
    pattern = pattern_signal(df, atr=a, prior_day_high=prior_day_high, prior_day_low=prior_day_low)

    tech = weights["orb"] * orb + weights["trend"] * trend + weights["pattern"] * pattern

    # Dampen when trend strongly disagrees with ORB (countertrend breakout = lower quality)
    disagree = (orb != 0) & (trend != 0) & (orb * trend < 0) & (trend.abs() == 1)
    tech[disagree] = tech[disagree] * 0.5

    out = pd.DataFrame({
        "orb": orb, "trend": trend, "pattern": pattern,
        "atr": a, "tech_score": tech.clip(-1.0, 1.0),
    }, index=df.index)
    return out
