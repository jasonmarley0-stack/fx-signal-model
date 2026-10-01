"""Classic candlestick reversal/continuation pattern detection.

Detects: bullish/bearish engulfing, hammer/shooting-star (pin bars), and
morning/evening star. Weighted by pattern reliability and whether it occurs
near a "significant level" (prior day high/low) for confluence.
"""
from __future__ import annotations
import pandas as pd

PATTERN_WEIGHTS = {
    "engulfing": 1.0,
    "pin_bar": 0.8,
    "star": 0.9,
}


def _body(df):
    return (df["close"] - df["open"]).abs()


def _range(df):
    return df["high"] - df["low"]


def _is_bullish(df):
    return df["close"] > df["open"]


def _near_level(price: pd.Series, level: pd.Series, atr: pd.Series, tolerance_mult: float = 0.5) -> pd.Series:
    return (price - level).abs() <= tolerance_mult * atr


def detect_engulfing(df: pd.DataFrame) -> pd.Series:
    prev_body = _body(df).shift(1)
    prev_bullish = _is_bullish(df).shift(1, fill_value=False)
    cur_bullish = _is_bullish(df)
    bull_engulf = (~prev_bullish) & cur_bullish & (df["close"] >= df["open"].shift(1)) & (df["open"] <= df["close"].shift(1)) & (_body(df) > prev_body)
    bear_engulf = prev_bullish & (~cur_bullish) & (df["open"] >= df["close"].shift(1)) & (df["close"] <= df["open"].shift(1)) & (_body(df) > prev_body)
    score = pd.Series(0.0, index=df.index)
    score[bull_engulf] = 1.0
    score[bear_engulf] = -1.0
    return score


def detect_pin_bar(df: pd.DataFrame, wick_body_ratio: float = 2.0) -> pd.Series:
    body = _body(df).replace(0, 1e-9)
    upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
    lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
    hammer = (lower_wick / body >= wick_body_ratio) & (upper_wick < body)
    shooting_star = (upper_wick / body >= wick_body_ratio) & (lower_wick < body)
    score = pd.Series(0.0, index=df.index)
    score[hammer] = 1.0
    score[shooting_star] = -1.0
    return score


def detect_star(df: pd.DataFrame) -> pd.Series:
    """Simplified 3-candle morning/evening star."""
    c1_bull_raw = _is_bullish(df).shift(2, fill_value=False)
    c1_bear = ~c1_bull_raw
    c1_body = _body(df).shift(2)
    c2_small = _body(df).shift(1) < 0.3 * c1_body
    c3_bull = _is_bullish(df) & (df["close"] > df["open"].shift(2) + 0.5 * c1_body)
    morning_star = c1_bear & c2_small & c3_bull

    c1_bull = c1_bull_raw
    c3_bear = (~_is_bullish(df)) & (df["close"] < df["open"].shift(2) - 0.5 * c1_body)
    evening_star = c1_bull & c2_small & c3_bear

    score = pd.Series(0.0, index=df.index)
    score[morning_star] = 1.0
    score[evening_star] = -1.0
    return score


def pattern_signal(df: pd.DataFrame, atr: pd.Series, prior_day_high: pd.Series | None = None,
                    prior_day_low: pd.Series | None = None) -> pd.Series:
    """Composite pattern score in roughly [-1, 1], boosted near significant levels."""
    engulf = detect_engulfing(df) * PATTERN_WEIGHTS["engulfing"]
    pin = detect_pin_bar(df) * PATTERN_WEIGHTS["pin_bar"]
    star = detect_star(df) * PATTERN_WEIGHTS["star"]

    combined = pd.concat([engulf, pin, star], axis=1).sum(axis=1)
    combined = combined.clip(-1.0, 1.0)

    if prior_day_high is not None and prior_day_low is not None:
        near_high = _near_level(df["close"], prior_day_high, atr)
        near_low = _near_level(df["close"], prior_day_low, atr)
        confluence_boost = pd.Series(1.0, index=df.index)
        confluence_boost[near_high | near_low] = 1.25
        combined = (combined * confluence_boost).clip(-1.0, 1.0)

    return combined
