"""Prototype: FX carry — long the higher-yielding currency, short the
lower-yielding one, collecting the daily interest-rate differential rather
than betting on direction at all. This is the "income, not patience"
avenue from the 2026-09-25 discussion — carry pays you for simply holding
the position, whether or not you're any good at predicting price.

Honest data limitation, stated up front rather than glossed over: OANDA's
API exposes only CURRENT financing rates (AccountInstruments), not a
historical time series. There is no way to know, from this data source,
what the real rate differential was in e.g. 2012. Two things are reported
separately so this limitation can't quietly bias the conclusion:

  1. PRICE-ONLY return: holding the pair in today's carry direction,
     historically, ignoring financing entirely — this uses only real
     historical spot data, no approximation, and tests the documented
     "carry currencies also tend to drift in the carry direction"
     phenomenon on its own.
  2. PRICE + APPROXIMATE FINANCING: adds today's live rate as a constant
     daily accrual across the whole backtest window — clearly an
     approximation (real differentials moved a lot, particularly the
     near-zero-rate 2009-2021 era vs. today), included for a sense of
     magnitude, not treated as historically accurate.

Universe: majors plus the classic JPY/CHF carry crosses — JPY and CHF have
been structurally low/negative-rate for most of the last two decades,
making them the traditional carry funding currencies; AUD/NZD the
traditional higher-yielders. Not cherry-picked for backtest performance —
chosen because they're the standard, pre-existing carry-trade pairs in
FX, same logic as choosing SPX500/gold for the trend prototype.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "trend_system"))

import os  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from oandapyV20 import API  # noqa: E402
from oandapyV20.endpoints.accounts import AccountInstruments  # noqa: E402

from backtest import fetch_daily, atr, stats, print_stats, TARGET_DAILY_VOL, MAX_POSITION_WEIGHT  # noqa: E402

UNIVERSE = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD", "NZD_USD",
    "AUD_JPY", "NZD_JPY", "AUD_CHF", "NZD_CHF", "GBP_JPY", "EUR_JPY",
]
SPREADS = {  # live snapshot, same convention as src/spreads.py / trend_system/instruments.py
    "EUR_USD": 0.000370, "GBP_USD": 0.000750, "USD_JPY": 0.057000, "USD_CHF": 0.000920,
    "AUD_USD": 0.001130, "USD_CAD": 0.000990, "NZD_USD": 0.001590,
    "AUD_JPY": 0.030000, "NZD_JPY": 0.035000, "AUD_CHF": 0.001800, "NZD_CHF": 0.001280,
    "GBP_JPY": 0.222000, "EUR_JPY": 0.152000,
}


def client() -> API:
    return API(access_token=os.environ["OANDA_API_KEY"], environment=os.environ.get("OANDA_ENVIRONMENT", "practice"))


def live_carry_direction_and_rate(instrument: str) -> tuple[int, float]:
    """Returns (direction, annual_rate) where direction=+1 means going
    long earns carry (longRate is the more positive/less negative side),
    -1 means going short does. annual_rate is that side's rate, as a
    fraction (e.g. 0.0209 = 2.09%/year)."""
    resp = client().request(AccountInstruments(accountID=os.environ["OANDA_ACCOUNT_ID"], params={"instruments": instrument}))
    fin = resp["instruments"][0]["financing"]
    long_rate, short_rate = float(fin["longRate"]), float(fin["shortRate"])
    if long_rate >= short_rate:
        return 1, long_rate
    return -1, short_rate


def backtest_pair(instrument: str, direction: int, annual_rate: float, spread: float) -> pd.DataFrame:
    df = fetch_daily(instrument)
    if len(df) < 250:
        return pd.DataFrame()
    close = df["close"]
    a = atr(df)
    daily_vol_frac = (a / close).clip(lower=1e-6)
    position_weight = (TARGET_DAILY_VOL / daily_vol_frac).clip(upper=MAX_POSITION_WEIGHT)
    price_return = close.pct_change().fillna(0.0)

    price_only_raw = direction * position_weight.shift(1).fillna(0) * price_return
    financing_daily = annual_rate / 365.0  # approximation — see module docstring
    price_plus_financing_raw = price_only_raw + position_weight.shift(1).fillna(0) * financing_daily

    cost = pd.Series(0.0, index=df.index)
    if len(cost) > 0:
        cost.iloc[0] = (spread / close.iloc[0]) * position_weight.iloc[0]  # one entry, held for the whole window

    return pd.DataFrame({
        "close": close,
        "price_only_net": price_only_raw - cost,
        "price_plus_financing_net": price_plus_financing_raw - cost,
    })


def main() -> None:
    print("Fetching live carry direction/rate per pair...\n")
    per_pair = {}
    for instrument in UNIVERSE:
        try:
            direction, rate = live_carry_direction_and_rate(instrument)
            side = "long" if direction == 1 else "short"
            print(f"  {instrument:10} carry side={side:5} rate={rate*100:+.2f}%/yr")
            df = backtest_pair(instrument, direction, rate, SPREADS[instrument])
            if not df.empty:
                per_pair[instrument] = df
        except Exception as ex:
            print(f"  {instrument}: failed — {ex}")

    print(f"\n=== Per-pair, PRICE-ONLY (real historical data, no rate approximation) ===")
    for instrument, df in per_pair.items():
        print_stats(stats(df["price_only_net"], instrument))

    print(f"\n=== Per-pair, PRICE + approximate financing (today's rate held constant) ===")
    for instrument, df in per_pair.items():
        print_stats(stats(df["price_plus_financing_net"], instrument))

    for label, key in [("PRICE-ONLY", "price_only_net"), ("PRICE + approx financing", "price_plus_financing_net")]:
        combined = pd.concat({k: v[key] for k, v in per_pair.items()}, axis=1)
        n_active = combined.notna().sum(axis=1).clip(lower=1)
        portfolio = combined.fillna(0).sum(axis=1) / n_active
        print(f"\n=== PORTFOLIO, {label} ===")
        print_stats(stats(portfolio, f"PORTFOLIO ({label})"))


if __name__ == "__main__":
    main()
