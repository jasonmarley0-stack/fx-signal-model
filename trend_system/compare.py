"""Two follow-up checks on the 2026-09-25 trend-following backtest:

1. Metals + equity indices ONLY (drop the 7 FX majors, which lost money on
   6 of 7 legs) — does the sub-portfolio that actually worked hold up on
   its own, or was some of its apparent quality just diversification
   averaging against the FX drag?

2. Buy-and-hold benchmark, same vol-targeted sizing, same cost model, just
   always long (never flips short) — isolates whether the EMA50/200 trend
   signal is adding real timing skill over just holding the (mostly
   secularly rising) asset, or whether the earlier good Sharpes were
   largely just that drift.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from instruments import UNIVERSE  # noqa: E402
from backtest import fetch_daily, atr, stats, print_stats, TARGET_DAILY_VOL, MAX_POSITION_WEIGHT, SLOW_EMA  # noqa: E402

NON_FX = {k: v for k, v in UNIVERSE.items() if v["class"] != "fx"}


def buy_and_hold(instrument: str, spread: float) -> pd.DataFrame:
    df = fetch_daily(instrument)
    if len(df) < SLOW_EMA + 20:
        return pd.DataFrame()
    close = df["close"]
    a = atr(df)
    daily_vol_frac = (a / close).clip(lower=1e-6)
    position_weight = (TARGET_DAILY_VOL / daily_vol_frac).clip(upper=MAX_POSITION_WEIGHT)
    price_return = close.pct_change().fillna(0.0)
    raw_return = position_weight.shift(1).fillna(0) * price_return  # always long — no signal flips
    cost = pd.Series(0.0, index=df.index)
    if len(cost) > 0:
        cost.iloc[0] = (spread / close.iloc[0]) * position_weight.iloc[0]  # one entry, held forever
    net_return = raw_return - cost
    return pd.DataFrame({"close": close, "raw_return": raw_return, "cost": cost, "net_return": net_return})


def portfolio_stats(per_instrument: dict, key: str, label: str) -> None:
    combined = pd.concat({k: v[key] for k, v in per_instrument.items()}, axis=1)
    n_active = combined.notna().sum(axis=1).clip(lower=1)
    portfolio = combined.fillna(0).sum(axis=1) / n_active
    print_stats(stats(portfolio, label))


def main() -> None:
    print("=== 1. Metals + equity indices ONLY (trend-following) ===\n")
    from backtest import backtest_instrument
    trend_non_fx = {}
    for instrument, meta in NON_FX.items():
        df = backtest_instrument(instrument, meta["spread"])
        if not df.empty:
            trend_non_fx[instrument] = df
    for instrument, df in trend_non_fx.items():
        print_stats(stats(df["net_return"], f"{instrument} (trend)"))
    print()
    portfolio_stats(trend_non_fx, "raw_return", "PORTFOLIO raw (no costs)")
    portfolio_stats(trend_non_fx, "net_return", "PORTFOLIO cost-adjusted")

    print("\n=== 2. Buy-and-hold benchmark (same sizing/cost model, never flips) ===\n")
    bh = {}
    for instrument, meta in UNIVERSE.items():
        df = buy_and_hold(instrument, meta["spread"])
        if not df.empty:
            bh[instrument] = df
    for instrument in UNIVERSE:
        if instrument not in bh:
            continue
        print_stats(stats(bh[instrument]["net_return"], f"{instrument} (buy&hold)"))

    print("\n=== Trend vs buy-and-hold, side by side (cost-adjusted) ===\n")
    from backtest import backtest_instrument as bt
    for instrument, meta in UNIVERSE.items():
        trend_df = bt(instrument, meta["spread"])
        bh_df = bh.get(instrument)
        if trend_df.empty or bh_df is None or bh_df.empty:
            continue
        t = stats(trend_df["net_return"], f"{instrument} trend")
        b = stats(bh_df["net_return"], f"{instrument} buy&hold")
        t_cagr = f"{t['cagr']*100:+.2f}%" if t["cagr"] is not None else "n/a"
        b_cagr = f"{b['cagr']*100:+.2f}%" if b["cagr"] is not None else "n/a"
        t_sharpe = f"{t['sharpe']:.2f}" if t["sharpe"] is not None else "n/a"
        b_sharpe = f"{b['sharpe']:.2f}" if b["sharpe"] is not None else "n/a"
        print(f"{instrument:12} trend: CAGR={t_cagr:>8} sharpe={t_sharpe:>5}   "
              f"buy&hold: CAGR={b_cagr:>8} sharpe={b_sharpe:>5}")

    print("\n=== Buy-and-hold PORTFOLIO, metals+equities only (vs trend PORTFOLIO above) ===\n")
    bh_non_fx = {k: v for k, v in bh.items() if k in NON_FX}
    portfolio_stats(bh_non_fx, "net_return", "B&H PORTFOLIO (metals+equities)")


if __name__ == "__main__":
    main()
