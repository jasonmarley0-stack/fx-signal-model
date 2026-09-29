"""Loads the frozen local OANDA bid/ask dataset (research/offline_comparison/
data/raw/*.csv, fetched by fetch_research_data.py via the existing,
authorised OANDA connection on 2026-09-29 — read-only GET requests only,
no order placed). Provides bid/ask/mid OHLC views and the fixed
dev/holdout split every candidate in this milestone is evaluated against.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

DATA_DIR = Path(__file__).parent / "data" / "raw"
PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]

# Fixed BEFORE any candidate's results were inspected (see RESULTS.md
# provenance section). Warm-up is additional history before DEV_START so
# every candidate's indicators (EMA50/ATR14/etc.) are real by the first
# bar a signal is allowed to fire on — no candidate is allowed to look at
# a bar it could not actually have had at decision time.
WARMUP_START = datetime(2025, 10, 1, tzinfo=timezone.utc)
DEV_START = datetime(2025, 11, 15, tzinfo=timezone.utc)   # ~45 days of warm-up (270 H4 bars) ahead of it
DEV_END = datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)
HOLDOUT_START = datetime(2026, 7, 1, tzinfo=timezone.utc)
HOLDOUT_END = datetime(2026, 9, 27, 23, 59, 59, tzinfo=timezone.utc)  # last complete H4 bar in the fetched dataset


def load_candles(pair: str, granularity: str) -> pd.DataFrame:
    path = DATA_DIR / f"{pair}_{granularity}.csv"
    df = pd.read_csv(path, parse_dates=["time"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    return df


def mid_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Midpoint OHLC — what src/strategies/composite.py's technical_score()
    and src/combiner.py's combine_signal() expect (matching production,
    which only ever sees midpoint prices from data/oanda.py)."""
    return pd.DataFrame({
        "open": (df["bid_open"] + df["ask_open"]) / 2,
        "high": (df["bid_high"] + df["ask_high"]) / 2,
        "low": (df["bid_low"] + df["ask_low"]) / 2,
        "close": (df["bid_close"] + df["ask_close"]) / 2,
        "volume": df["volume"],
    }, index=df.index)


def bid_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={"bid_open": "open", "bid_high": "high", "bid_low": "low", "bid_close": "close"})[
        ["open", "high", "low", "close"]]


def ask_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={"ask_open": "open", "ask_high": "high", "ask_low": "low", "ask_close": "close"})[
        ["open", "high", "low", "close"]]


@dataclass
class PairData:
    pair: str
    h4: pd.DataFrame     # raw bid/ask H4
    m30: pd.DataFrame    # raw bid/ask M30
    h4_mid: pd.DataFrame
    m30_bid: pd.DataFrame
    m30_ask: pd.DataFrame


def load_pair(pair: str) -> PairData:
    h4 = load_candles(pair, "H4")
    m30 = load_candles(pair, "M30")
    return PairData(pair=pair, h4=h4, m30=m30, h4_mid=mid_ohlc(h4), m30_bid=bid_ohlc(m30), m30_ask=ask_ohlc(m30))


def load_all_pairs() -> dict[str, PairData]:
    return {pair: load_pair(pair) for pair in PAIRS}
