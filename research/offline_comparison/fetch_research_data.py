"""Read-only OANDA bid/ask candle fetch for the offline strategy-comparison
research milestone. Run once, on the droplet (where OANDA credentials
live), to build the frozen local dataset this research branch's harness
replays against. Makes GET requests only (InstrumentsCandles) -- no order
is placed, no account state is touched.

Usage (on the droplet, from /root/fx-signal-model):
    set -a; source setup/oanda.env; set +a
    .venv/bin/python3 research_fetch/fetch_research_data.py
"""
from __future__ import annotations
import sys
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, "src")

from data.oanda import _client, to_oanda_instrument  # noqa: E402
from oandapyV20.endpoints.instruments import InstrumentsCandles  # noqa: E402
import pandas as pd  # noqa: E402

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
GRANULARITIES = ["H4", "M30"]
START = datetime(2025, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 28, tzinfo=timezone.utc)
OUT_DIR = Path("/root/research_data")
GRAN_MINUTES = {"H4": 240, "M30": 30}


def fetch_ba_candles(pair: str, granularity: str, start: datetime, end: datetime) -> pd.DataFrame:
    client = _client()
    instrument = to_oanda_instrument(pair)
    rows = []
    cursor = start
    max_span = timedelta(minutes=GRAN_MINUTES[granularity] * 4500)  # stay under OANDA's 5000-candle cap
    while cursor < end:
        chunk_end = min(cursor + max_span, end)
        params = {
            "granularity": granularity, "price": "BA",
            "from": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        resp = client.request(InstrumentsCandles(instrument=instrument, params=params))
        for c in resp.get("candles", []):
            if not c.get("complete"):
                continue
            bid, ask = c["bid"], c["ask"]
            rows.append({
                "time": c["time"], "volume": c.get("volume", 0),
                "bid_open": float(bid["o"]), "bid_high": float(bid["h"]), "bid_low": float(bid["l"]), "bid_close": float(bid["c"]),
                "ask_open": float(ask["o"]), "ask_high": float(ask["h"]), "ask_low": float(ask["l"]), "ask_close": float(ask["c"]),
            })
        print(f"    chunk {cursor.date()}..{chunk_end.date()}: {len(resp.get('candles', []))} candles")
        cursor = chunk_end
        time.sleep(0.3)
    df = pd.DataFrame(rows)
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    return df


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []
    for pair in PAIRS:
        for gran in GRANULARITIES:
            print(f"fetching {pair} {gran} {START.date()}..{END.date()} ...")
            df = fetch_ba_candles(pair, gran, START, END)
            path = OUT_DIR / f"{pair}_{gran}.csv"
            df.to_csv(path, index=False)
            first = df["time"].iloc[0].isoformat() if not df.empty else None
            last = df["time"].iloc[-1].isoformat() if not df.empty else None
            print(f"  wrote {len(df)} rows to {path} (first={first}, last={last})")
            manifest.append({"pair": pair, "granularity": gran, "rows": len(df), "first": first, "last": last})
    pd.DataFrame(manifest).to_csv(OUT_DIR / "manifest.csv", index=False)
    print("done. manifest:")
    print(pd.DataFrame(manifest).to_string(index=False))


if __name__ == "__main__":
    main()
