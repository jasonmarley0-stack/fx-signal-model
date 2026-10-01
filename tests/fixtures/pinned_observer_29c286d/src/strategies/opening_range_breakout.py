"""30-minute Opening Range Breakout strategy.

Expects a DataFrame of 30-min OHLC bars, timezone-aware index (UTC),
with columns: open, high, low, close, volume (volume optional).

Produces a score column in [-1, 0, 1] per bar for a given trading session.
"""
from __future__ import annotations
import pandas as pd
from .indicators import atr
from sessions import SESSIONS, session_open_datetime

# Kept for backwards compatibility with callers passing a session name string;
# the actual open time is now computed per-date (DST-aware) via sessions.py
# rather than a fixed UTC clock time — a fixed "07:00 UTC" for London, for
# example, is only correct during British Summer Time and silently uses the
# wrong hour (an hour too early) for the whole GMT/winter half of the year.
SESSION_OPENS_UTC = {"london": "london", "new_york": "new_york"}


def compute_opening_range(df: pd.DataFrame, session_key: str) -> pd.DataFrame:
    """Tags each bar with that day's opening-range high/low for the given
    session, using that session's real DST-adjusted local open time."""
    df = df.copy()
    df["date"] = df.index.date
    or_high, or_low = {}, {}
    for date, day_df in df.groupby("date"):
        open_ts = session_open_datetime(session_key, date)
        window = day_df[(day_df.index >= open_ts) & (day_df.index < open_ts + pd.Timedelta(minutes=30))]
        if len(window) == 0:
            continue
        or_high[date] = window["high"].max()
        or_low[date] = window["low"].min()
    df["or_high"] = df["date"].map(or_high)
    df["or_low"] = df["date"].map(or_low)
    return df.drop(columns=["date"])


def orb_signal(df: pd.DataFrame, session_open: str = "london",
               atr_period: int = 14, min_atr_multiple: float = 0.25,
               confirm_bars: int = 2) -> pd.Series:
    """Returns a Series of scores in {-1, 0, 1} aligned to df.index.

    Rules:
      - Breakout candle must CLOSE beyond the opening range (not just wick).
      - Breakout size must exceed min_atr_multiple * ATR14 (filters noise breakouts).
      - Requires `confirm_bars` consecutive closes beyond the range (2-candle confirmation).
      - Fires ONCE per session, on the bar where confirmation first completes —
        not on every subsequent bar while price stays extended. (An earlier
        version kept signaling +1/-1 on every bar for as long as price stayed
        beyond the range, which meant "entering" repeatedly deep into an
        already-extended move rather than catching the actual breakout.)

    `session_open` accepts a session key from sessions.SESSIONS ("london",
    "new_york", "tokyo", "sydney").
    """
    session_key = session_open if session_open in SESSIONS else "london"
    tagged = compute_opening_range(df, session_key)
    a = atr(df, atr_period)

    raw_long = (tagged["close"] > tagged["or_high"]) & (
        (tagged["close"] - tagged["or_high"]) > min_atr_multiple * a
    )
    raw_short = (tagged["close"] < tagged["or_low"]) & (
        (tagged["or_low"] - tagged["close"]) > min_atr_multiple * a
    )

    confirmed_long = raw_long.rolling(confirm_bars).sum() >= confirm_bars
    confirmed_short = raw_short.rolling(confirm_bars).sum() >= confirm_bars

    # One-shot per session: only the first bar of each day where confirmation
    # is newly true (not already true on the previous bar) fires a signal.
    first_long = confirmed_long & ~confirmed_long.shift(1, fill_value=False)
    first_short = confirmed_short & ~confirmed_short.shift(1, fill_value=False)

    score = pd.Series(0, index=df.index, dtype=float)
    score[first_long] = 1.0
    score[first_short] = -1.0
    return score
