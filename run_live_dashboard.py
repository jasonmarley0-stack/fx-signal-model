"""Builds the morning dashboard from REAL data end to end:
- real recent OHLC per pair via Dukascopy (src/data/dukascopy.py)
- real PESTLE scores via a running Signal Engine instance (SIGNAL_ENGINE_BASE_URL)

This is the live counterpart to run_demo.py, which intentionally uses
synthetic data everywhere as a fast smoke test. Use this once you're ready
to see the actual dashboard for today.

Usage (from the repo root, with SIGNAL_ENGINE_BASE_URL set and your venv active):
    python3 run_live_dashboard.py [--days 14] [--pairs EURUSD,GBPUSD,...]
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent / "dashboard"))

from backtest import load_real_ohlc  # noqa: E402
from dashboard_data import build_dashboard_payload, DEFAULT_PAIRS  # noqa: E402
from generate_dashboard import render_dashboard  # noqa: E402
from pestle.signal_engine_client import SignalEngineClient  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14, help="How many days of real OHLC to pull per pair")
    parser.add_argument("--pairs", type=str, default=",".join(DEFAULT_PAIRS))
    args = parser.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]

    client = SignalEngineClient()
    print(f"Signal Engine mock mode: {client.mock}"
          + ("  (set SIGNAL_ENGINE_BASE_URL to use real PESTLE data)" if client.mock else ""))

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)

    pair_dfs = {}
    for pair in pairs:
        print(f"Pulling {args.days}d of real {pair} data from Dukascopy...")
        df = load_real_ohlc(pair, start, end, freq="30min")
        print(f"  {pair}: {len(df)} bars")
        if not df.empty:
            pair_dfs[pair] = df

    if not pair_dfs:
        print("No real data loaded for any pair — check network access to "
              "datafeed.dukascopy.com from this machine.")
        sys.exit(1)

    payload = build_dashboard_payload(pair_dfs, pairs=list(pair_dfs.keys()))

    def serialize(row):
        sig = row["signal"]
        row = dict(row)
        window = None
        if sig.window:
            window = dict(sig.window)
            window["generated_at_utc"] = window["generated_at_utc"].isoformat()
            window["valid_until_utc"] = window["valid_until_utc"].isoformat()
        row["signal"] = {
            "direction": sig.direction, "confidence": sig.confidence,
            "combined_score": sig.combined_score, "entry": sig.entry,
            "stop_loss_range": list(sig.stop_loss_range),
            "take_profit_range": list(sig.take_profit_range), "reason": sig.reason,
            "window": window,
        }
        return row

    payload["rows"] = [serialize(r) for r in payload["rows"]]
    Path("live_dashboard_payload.json").write_text(json.dumps(payload, indent=2, default=str))

    html = render_dashboard(payload)
    Path("live_morning_dashboard.html").write_text(html)
    print(f"\nBuilt live dashboard for {len(payload['rows'])} pairs -> live_morning_dashboard.html")


if __name__ == "__main__":
    main()
