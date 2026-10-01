"""Live OANDA candle fetcher — the droplet's real-time data source, parallel
to src/data/dukascopy.py (which stays the backtester's historical source;
Dukascopy access was the Mac-only constraint, this is what replaces it for
live scanning on the droplet).

Needs OANDA_API_KEY and OANDA_ACCOUNT_ID in the environment (see
setup/oanda.env on the droplet — loaded into the scanner service via
systemd's EnvironmentFile=). Defaults to OANDA's practice endpoint; set
OANDA_ENVIRONMENT=live to point at a funded live account instead.
"""
from __future__ import annotations
import os
from datetime import datetime
import pandas as pd
from oandapyV20 import API
from oandapyV20.endpoints.instruments import InstrumentsCandles

# MODEL_SPEC pairs use plain "EURUSD"; OANDA instruments use "EUR_USD".
def to_oanda_instrument(pair: str) -> str:
    return f"{pair[:3]}_{pair[3:]}"


def _client() -> API:
    api_key = os.environ.get("OANDA_API_KEY")
    if not api_key:
        raise RuntimeError("OANDA_API_KEY not set in environment — check setup/oanda.env is loaded")
    environment = os.environ.get("OANDA_ENVIRONMENT", "practice")
    return API(access_token=api_key, environment=environment)


def fetch_oanda_candles(pair: str, count: int | None = 300, granularity: str = "M30",
                         from_time: datetime | None = None, to_time: datetime | None = None) -> pd.DataFrame:
    """Returns a DataFrame with open/high/low/close columns and a tz-aware
    UTC DatetimeIndex — same shape backtest.load_real_ohlc produces, so it
    drops straight into strategies.composite.technical_score unchanged.
    Only completed candles are included (the in-progress current candle is
    dropped, matching the backtester's convention of scoring closed bars).

    Default mode (from_time/to_time both None) fetches the most recent
    `count` bars — the live-scanning use case. Pass from_time/to_time
    (tz-aware UTC datetimes) instead to fetch a specific historical range —
    e.g. performance_scorer.py checking what price actually did after a
    past signal fired — rather than "most recent N bars"."""
    client = _client()
    instrument = to_oanda_instrument(pair)
    params = {"granularity": granularity, "price": "M"}  # midpoint OHLC
    if from_time is not None or to_time is not None:
        if from_time is not None:
            params["from"] = from_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        if to_time is not None:
            params["to"] = to_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        params["count"] = count if count is not None else 300
    resp = client.request(InstrumentsCandles(instrument=instrument, params=params))

    rows = []
    for candle in resp.get("candles", []):
        if not candle.get("complete"):
            continue
        mid = candle["mid"]
        rows.append({
            "time": candle["time"],
            "open": float(mid["o"]), "high": float(mid["h"]),
            "low": float(mid["l"]), "close": float(mid["c"]),
            # OANDA spot FX has no true traded volume (it's OTC) — this is
            # tick count per candle, a proxy for market activity/liquidity,
            # not volume in the equity-market sense. Used to gate false
            # breakouts in low-activity conditions (see strategies backtest
            # in NEXT_STEPS.md), not as a standalone directional signal.
            "volume": int(candle.get("volume", 0)),
        })
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    return df


def fetch_current_price(pair: str) -> float:
    """Latest mid price — used as the signal's entry price, since the most
    recent completed candle's close can lag by up to one bar.

    count=1 here used to mean "the single most recent M1 candle" — which is
    very often the one still forming right now, and fetch_oanda_candles
    already drops incomplete candles, so that request would come back empty
    on a normal cadence, not just at the edges. Fetching a few and taking
    the last (now guaranteed complete) row fixes it without changing the
    "latest available price" intent — confirmed live 2026-09-01, this was
    failing on most pairs, most of the time, not as an edge case."""
    df = fetch_oanda_candles(pair, count=5, granularity="M1")
    if df.empty:
        raise RuntimeError(f"No live price available for {pair}")
    return float(df["close"].iloc[-1])
