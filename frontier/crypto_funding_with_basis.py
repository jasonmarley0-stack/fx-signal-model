"""Crypto funding harvest, now including basis risk — the one component
still missing after the fee correction. Long spot + short perp is only
truly riskless on the price side if the two legs move in perfect lockstep;
in reality they don't, and that gap (the "basis") adds real return
variance a pure funding-rate backtest can't see.

Modeled directly and exactly, not approximated: fetch real, time-aligned
8h spot and perp candles (Binance's 8h kline boundaries land exactly on
funding timestamps — checked, not assumed), and for every period the
position is held, the realized return is:

    funding_rate_t + (spot_return_t - perp_return_t)

If the legs tracked perfectly, (spot_return_t - perp_return_t) would be
~0 every period and this would reduce to the funding-only backtest. It
won't be exactly zero in reality — that gap IS the basis risk, appearing
here as real, additional variance rather than an assumption.
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import requests  # noqa: E402
import pandas as pd  # noqa: E402

from crypto_funding import fetch_all_funding, stats, print_stats, COST_VARIANTS, MIN_PERSISTENCE, FUNDING_INTERVALS_PER_YEAR  # noqa: E402

SYMBOLS = ["BTCUSDT", "ETHUSDT"]


def fetch_klines(base_url: str, symbol: str, start_time: int) -> pd.Series:
    """Paginates 8h klines from either the spot or futures endpoint,
    returns closing price indexed by the candle's open time (which is
    exactly the funding timestamp for that period)."""
    all_rows = []
    while True:
        params = {"symbol": symbol, "interval": "8h", "limit": 1000, "startTime": start_time}
        resp = requests.get(base_url, params=params, timeout=30)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < 1000:
            break
        start_time = rows[-1][0] + 1
        time.sleep(0.2)
    s = pd.Series(
        {pd.to_datetime(r[0], unit="ms", utc=True): float(r[4]) for r in all_rows}  # index 4 = close
    )
    return s.sort_index()


def walk_with_basis(funding: pd.Series, spot_return: pd.Series, perp_return: pd.Series,
                     round_trip_cost: float) -> pd.Series:
    net_return = pd.Series(0.0, index=funding.index)
    state_on = False
    pending_sign = None
    pending_run = 0
    for t, rate in funding.items():
        sign_positive = rate > 0
        if sign_positive == state_on:
            pending_run = 0
        else:
            if pending_sign == sign_positive:
                pending_run += 1
            else:
                pending_sign, pending_run = sign_positive, 1
            if pending_run >= MIN_PERSISTENCE:
                net_return.loc[t] -= round_trip_cost
                state_on = sign_positive
                pending_sign, pending_run = None, 0
        if state_on:
            basis_pnl = spot_return.get(t, 0.0) - perp_return.get(t, 0.0)
            net_return.loc[t] += rate + basis_pnl
    return net_return


def main() -> None:
    start_time = 1483228800000  # 2017-01-01 UTC, same as crypto_funding.py
    results = {}
    for symbol in SYMBOLS:
        print(f"Fetching funding + aligned spot/perp candles for {symbol}...")
        funding = fetch_all_funding(symbol)
        spot_close = fetch_klines("https://api.binance.com/api/v3/klines", symbol, start_time)
        perp_close = fetch_klines("https://fapi.binance.com/fapi/v1/klines", symbol, start_time)
        spot_return = spot_close.pct_change().fillna(0.0)
        perp_return = perp_close.pct_change().fillna(0.0)

        common = funding.index.intersection(spot_return.index).intersection(perp_return.index)
        print(f"  {len(funding)} funding events, {len(common)} with aligned spot+perp price data")

        basis_gap = (spot_return.loc[common] - perp_return.loc[common])
        print(f"  tracking gap (spot_return - perp_return) per period: "
              f"mean={basis_gap.mean()*100:+.4f}% std={basis_gap.std()*100:.4f}%")

        results[symbol] = (funding.loc[common], spot_return.loc[common], perp_return.loc[common])

    for variant_name, cost in COST_VARIANTS.items():
        print(f"\n########## {variant_name}: round_trip_cost={cost*100:.3f}% (WITH basis risk) ##########")
        per_symbol = {}
        for symbol, (funding, spot_return, perp_return) in results.items():
            net_return = walk_with_basis(funding, spot_return, perp_return, cost)
            per_symbol[symbol] = net_return
            print_stats(stats(net_return, FUNDING_INTERVALS_PER_YEAR, f"{symbol} net"))

        combined = pd.concat(per_symbol, axis=1)
        n_active = combined.notna().sum(axis=1).clip(lower=1)
        portfolio = combined.fillna(0).sum(axis=1) / n_active
        print_stats(stats(portfolio, FUNDING_INTERVALS_PER_YEAR, "PORTFOLIO"))


if __name__ == "__main__":
    main()
