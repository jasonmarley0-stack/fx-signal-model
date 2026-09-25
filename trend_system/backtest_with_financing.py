"""Trend + buy-and-hold backtests corrected to include ongoing financing
cost, not just the one-time entry spread — a real gap found 2026-09-25:
OANDA's live financing rates show holding gold/silver/equity-index CFDs
LONG costs 5.6-6.4%/year, while holding them SHORT actually earns a small
credit (+1.4% to +3.2%/year). The original trend_system/backtest.py and
compare.py never modeled this at all, only the one-time spread — a
meaningful blind spot for any position held for weeks to months, exactly
the holding period this whole prototype is built around.

Same disclosed limitation as the carry system's rates: OANDA only exposes
CURRENT financing rates via the API, not history, so today's rate is held
constant across the ~20-year backtest window. Real rates moved over time;
this is a magnitude estimate, not a point-in-time-correct reconstruction.

Asymmetric by design — long and short rates are NOT mirror images of each
other (unlike the spread cost), so this applies the correct signed rate
for whichever side is actually held each day, not a generic symmetric
drag.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import os  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from oandapyV20 import API  # noqa: E402
from oandapyV20.endpoints.accounts import AccountInstruments  # noqa: E402

from instruments import UNIVERSE  # noqa: E402
from backtest import (fetch_daily, atr, stats, print_stats, FAST_EMA, SLOW_EMA,  # noqa: E402
                       TARGET_DAILY_VOL, MAX_POSITION_WEIGHT)


def fetch_financing_rates() -> dict[str, tuple[float, float]]:
    """Returns {instrument: (long_rate, short_rate)} as annual fractions."""
    api_key = os.environ["OANDA_API_KEY"]
    client = API(access_token=api_key, environment=os.environ.get("OANDA_ENVIRONMENT", "practice"))
    account_id = os.environ["OANDA_ACCOUNT_ID"]
    names = ",".join(UNIVERSE.keys())
    resp = client.request(AccountInstruments(accountID=account_id, params={"instruments": names}))
    out = {}
    for i in resp["instruments"]:
        fin = i.get("financing", {})
        if "longRate" in fin and "shortRate" in fin:
            out[i["name"]] = (float(fin["longRate"]), float(fin["shortRate"]))
    return out


def backtest_instrument(instrument: str, spread: float, long_rate: float, short_rate: float) -> pd.DataFrame:
    df = fetch_daily(instrument)
    if len(df) < SLOW_EMA + 20:
        return pd.DataFrame()
    close = df["close"]
    ema_fast = close.ewm(span=FAST_EMA, adjust=False).mean()
    ema_slow = close.ewm(span=SLOW_EMA, adjust=False).mean()
    trend_signal = pd.Series(np.where(ema_fast > ema_slow, 1, -1), index=df.index)

    a = atr(df)
    daily_vol_frac = (a / close).clip(lower=1e-6)
    position_weight = (TARGET_DAILY_VOL / daily_vol_frac).clip(upper=MAX_POSITION_WEIGHT)
    price_return = close.pct_change().fillna(0.0)

    def run(signal: pd.Series) -> pd.DataFrame:
        raw_return = signal.shift(1).fillna(0) * position_weight.shift(1).fillna(0) * price_return
        applicable_rate = signal.shift(1).apply(lambda s: long_rate if s == 1 else (short_rate if s == -1 else 0.0))
        financing = position_weight.shift(1).fillna(0) * (applicable_rate.fillna(0) / 365.0)
        changed = signal != signal.shift(1)
        spread_cost = pd.Series(0.0, index=df.index)
        spread_cost[changed] = (spread / close[changed]) * position_weight.shift(1).fillna(0)[changed]
        net = raw_return + financing - spread_cost
        return pd.DataFrame({"raw_return": raw_return, "financing": financing,
                             "spread_cost": spread_cost, "net_return": net})

    trend = run(trend_signal)
    buy_hold_signal = pd.Series(1, index=df.index)
    bh = run(buy_hold_signal)
    return trend, bh


def main() -> None:
    print("Fetching live financing rates...")
    rates = fetch_financing_rates()
    for inst, (lr, sr) in rates.items():
        print(f"  {inst:12} long={lr*100:+.2f}%/yr  short={sr*100:+.2f}%/yr")

    trend_results, bh_results = {}, {}
    print("\nBacktesting with financing cost included...")
    for instrument, meta in UNIVERSE.items():
        if instrument not in rates:
            print(f"  {instrument}: no financing rate available, skipping")
            continue
        long_rate, short_rate = rates[instrument]
        try:
            trend_df, bh_df = backtest_instrument(instrument, meta["spread"], long_rate, short_rate)
            if trend_df is None or trend_df.empty:
                print(f"  {instrument}: insufficient data")
                continue
            trend_results[instrument] = trend_df
            bh_results[instrument] = bh_df
            print(f"  {instrument}: done")
        except Exception as ex:
            print(f"  {instrument}: failed — {ex}")

    print("\n=== TREND, with financing (cost-adjusted) ===")
    for instrument, df in trend_results.items():
        print_stats(stats(df["net_return"], instrument))
    combined = pd.concat({k: v["net_return"] for k, v in trend_results.items()}, axis=1)
    n_active = combined.notna().sum(axis=1).clip(lower=1)
    print_stats(stats(combined.fillna(0).sum(axis=1) / n_active, "PORTFOLIO (trend, w/ financing)"))

    print("\n=== BUY-AND-HOLD, with financing (cost-adjusted) ===")
    for instrument, df in bh_results.items():
        print_stats(stats(df["net_return"], instrument))
    combined_bh = pd.concat({k: v["net_return"] for k, v in bh_results.items()}, axis=1)
    n_active_bh = combined_bh.notna().sum(axis=1).clip(lower=1)
    print_stats(stats(combined_bh.fillna(0).sum(axis=1) / n_active_bh, "PORTFOLIO (buy&hold, w/ financing)"))

    print("\n=== Financing drag alone, buy-and-hold, cumulative over the full period (return-fraction units) ===")
    for instrument, df in bh_results.items():
        print(f"  {instrument:12} cumulative financing = {df['financing'].sum():+.3f}")


if __name__ == "__main__":
    main()
