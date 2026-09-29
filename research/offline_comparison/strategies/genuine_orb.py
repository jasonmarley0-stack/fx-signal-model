"""Candidate 2: a genuinely-computed session opening range, measured from
real M30 bars, available only after the opening M30 bar has closed --
NOT a relabelled H4 candle.

This is a NEW implementation. It does not modify
src/strategies/opening_range_breakout.py, whose compute_opening_range()
has an already-documented, confirmed bug (CODEX_FOLLOWUP_FINDINGS.md item
6 / the read-only audit): when called with H4 bars (as live_scanner.py
does via composite.technical_score), its 30-minute window essentially
never contains any H4 bar's own timestamp, so ORB is permanently 0 on the
current live scanner. This candidate exists specifically to test whether
a correctly-computed opening range helps -- it must not reproduce that
same defect under a new name.
"""
from __future__ import annotations
import sys
from datetime import timedelta
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from sessions import PRIMARY_SESSION, session_open_datetime  # noqa: E402
from strategies.indicators import atr  # noqa: E402
from strategies.trend_following import trend_signal  # noqa: E402
from strategies.candlestick_patterns import pattern_signal  # noqa: E402

MIN_ATR_MULTIPLE = 0.25  # same filter threshold the existing (broken) orb_signal uses, kept for comparability -- not tuned here
WEIGHTS = {"orb": 0.5, "trend": 0.3, "pattern": 0.2}  # identical to composite.py's DEFAULT_WEIGHTS -- only the orb input changes


def compute_genuine_opening_range(m30_mid: pd.DataFrame, pair: str) -> pd.DataFrame:
    """One row per calendar date: or_high, or_low, and available_from_utc
    -- the timestamp at/after which this day's range may be used (the
    opening M30 bar's own close, never before)."""
    session_key = PRIMARY_SESSION.get(pair, "london")
    rows = []
    for date in sorted(set(m30_mid.index.date)):
        open_ts = session_open_datetime(session_key, date)
        window = m30_mid[(m30_mid.index >= open_ts) & (m30_mid.index < open_ts + timedelta(minutes=30))]
        if window.empty:
            continue
        rows.append({
            "date": date, "or_high": window["high"].max(), "or_low": window["low"].min(),
            "available_from_utc": open_ts + timedelta(minutes=30),
        })
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame(
        columns=["or_high", "or_low", "available_from_utc"])


def genuine_orb_signal(h4_mid: pd.DataFrame, m30_mid: pd.DataFrame, pair: str) -> pd.Series:
    """Score in {-1, 0, 1} aligned to h4_mid.index. An H4 bar may only be
    scored against a day's range once that range's available_from_utc is
    at or before the H4 bar's own OPEN time -- never its close, which
    would let the bar see data from within itself. Fires once per
    calendar date (first qualifying H4 bar), mirroring the existing
    orb_signal's one-shot-per-session convention."""
    or_table = compute_genuine_opening_range(m30_mid, pair)
    a = atr(h4_mid, 14)
    score = pd.Series(0.0, index=h4_mid.index)
    fired_dates: set = set()
    # An OANDA candle's `time` IS its open time, so ts itself is the bar's open.
    for ts, bar in h4_mid.iterrows():
        date = ts.date()
        if date in fired_dates or date not in or_table.index:
            continue
        row = or_table.loc[date]
        if ts < row["available_from_utc"]:
            continue
        atr_val = a.loc[ts]
        if pd.isna(atr_val) or atr_val == 0:
            continue
        broke_up = (bar["high"] > row["or_high"]) and ((bar["high"] - row["or_high"]) > MIN_ATR_MULTIPLE * atr_val)
        broke_down = (bar["low"] < row["or_low"]) and ((row["or_low"] - bar["low"]) > MIN_ATR_MULTIPLE * atr_val)
        if broke_up and broke_down:
            continue  # both sides broken within one bar -- ambiguous, no direction assumed
        if broke_up:
            score.loc[ts] = 1.0
            fired_dates.add(date)
        elif broke_down:
            score.loc[ts] = -1.0
            fired_dates.add(date)
    return score


def genuine_orb_technical_score(h4_mid: pd.DataFrame, m30_mid: pd.DataFrame, pair: str) -> pd.DataFrame:
    """Same output shape as strategies.composite.technical_score(): orb,
    trend, pattern, atr, tech_score -- trend/pattern reused unmodified
    from the existing strategy modules; only the orb input is replaced."""
    a = atr(h4_mid, 14)
    df = h4_mid.copy()
    df["date"] = df.index.date
    daily = df.groupby("date").agg(day_high=("high", "max"), day_low=("low", "min"))
    prior_day_high = df["date"].map(daily["day_high"].shift(1))
    prior_day_low = df["date"].map(daily["day_low"].shift(1))
    df.drop(columns=["date"], inplace=True)

    orb = genuine_orb_signal(h4_mid, m30_mid, pair)
    trend = trend_signal(h4_mid)
    pattern = pattern_signal(h4_mid, atr=a, prior_day_high=prior_day_high, prior_day_low=prior_day_low)

    tech = WEIGHTS["orb"] * orb + WEIGHTS["trend"] * trend + WEIGHTS["pattern"] * pattern
    disagree = (orb != 0) & (trend != 0) & (orb * trend < 0) & (trend.abs() == 1)
    tech[disagree] = tech[disagree] * 0.5

    return pd.DataFrame({"orb": orb, "trend": trend, "pattern": pattern, "atr": a,
                          "tech_score": tech.clip(-1.0, 1.0)}, index=h4_mid.index)
