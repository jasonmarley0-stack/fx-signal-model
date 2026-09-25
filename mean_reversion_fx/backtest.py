"""Prototype: mean-reversion on FX majors — the mirror image of the
trend-following prototype, testing the opposite hypothesis. The
trend-following backtest found FX majors lost money on 6 of 7 legs
following a trend signal; that's suggestive that FX majors spend more time
mean-reverting than trending, which this tests directly rather than just
inferring it.

Signal: z-score of price vs its own 20-day moving average
(z = (close - SMA20) / rolling_std20). Enter short when price is stretched
more than 2 std devs above its recent average (bet it reverts down), enter
long when stretched more than 2 below. Exit when price reverts back through
its own average, or after MAX_HOLD_DAYS if reversion never comes — a real
risk with mean-reversion is that the stretch keeps extending ("catching a
falling knife"), so this isn't a strategy that should be allowed to hold
indefinitely waiting for a reversion that may not arrive.

Structurally different from the trend prototype in a way that matters for
the backtest engine, not just the signal: trend was "always in the
market" (flip long/short, never flat), so its returns could be computed
vectorized. This is opportunistic — flat most of the time, only in a
position when price is genuinely stretched — so it's walked day by day
like a real trade log, closer to how the FX signal product's own
performance_scorer.py works, and reports win_rate per completed trade on
top of the portfolio stats for direct comparability.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "trend_system"))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pandas as pd  # noqa: E402

from backtest import fetch_daily, atr, stats, print_stats, TARGET_DAILY_VOL, MAX_POSITION_WEIGHT  # noqa: E402
from spreads import SPREADS  # noqa: E402

UNIVERSE = ["EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD", "NZD_USD"]
SMA_PERIOD = 20
ENTRY_Z = 2.0
EXIT_Z = 0.0
MAX_HOLD_DAYS = 20


def spread_for(instrument: str) -> float:
    return SPREADS[instrument.replace("_", "")]


def backtest_pair(instrument: str, spread: float) -> tuple[pd.DataFrame, list[dict]]:
    df = fetch_daily(instrument)
    if len(df) < SMA_PERIOD + 30:
        return pd.DataFrame(), []
    close = df["close"]
    sma = close.rolling(SMA_PERIOD).mean()
    std = close.rolling(SMA_PERIOD).std()
    z = (close - sma) / std.replace(0, pd.NA)
    a = atr(df)
    daily_vol_frac = (a / close).clip(lower=1e-6)
    position_weight = (TARGET_DAILY_VOL / daily_vol_frac).clip(upper=MAX_POSITION_WEIGHT)
    price_return = close.pct_change().fillna(0.0)

    net_returns = pd.Series(0.0, index=df.index)
    trades = []
    position = 0  # 0 flat, +1 long, -1 short
    entry_price = None
    entry_weight = None
    days_in_trade = 0

    idx = df.index
    for i in range(1, len(idx)):
        today, yesterday = idx[i], idx[i - 1]
        zt = z.get(yesterday)  # decide today's action off yesterday's close — no lookahead

        if position != 0:
            net_returns.loc[today] += position * entry_weight * price_return.loc[today]
            days_in_trade += 1
            reverted = (position == 1 and zt is not None and zt >= EXIT_Z) or \
                       (position == -1 and zt is not None and zt <= EXIT_Z)
            timed_out = days_in_trade >= MAX_HOLD_DAYS
            if reverted or timed_out:
                exit_price = close.loc[today]
                net_returns.loc[today] -= (spread / exit_price) * entry_weight
                trades.append({"pair": instrument, "direction": "long" if position == 1 else "short",
                               "entry": entry_price, "exit": exit_price, "days_held": days_in_trade,
                               "reason": "reverted" if reverted else "timed_out",
                               "pnl_pct": position * (exit_price - entry_price) / entry_price})
                position, entry_price, entry_weight, days_in_trade = 0, None, None, 0

        if position == 0 and zt is not None:
            if zt > ENTRY_Z:
                position, entry_price, entry_weight, days_in_trade = -1, close.loc[yesterday], position_weight.loc[yesterday], 0
                net_returns.loc[today] -= (spread / entry_price) * entry_weight
            elif zt < -ENTRY_Z:
                position, entry_price, entry_weight, days_in_trade = 1, close.loc[yesterday], position_weight.loc[yesterday], 0
                net_returns.loc[today] -= (spread / entry_price) * entry_weight

    return pd.DataFrame({"close": close, "net_return": net_returns}), trades


def main() -> None:
    per_pair = {}
    all_trades = []
    print("Fetching daily history + walking mean-reversion trades...")
    for instrument in UNIVERSE:
        try:
            df, trades = backtest_pair(instrument, spread_for(instrument))
            if df.empty:
                print(f"  {instrument}: insufficient data")
                continue
            per_pair[instrument] = df
            all_trades.extend(trades)
            print(f"  {instrument}: {len(df)} bars, {len(trades)} completed trades")
        except Exception as ex:
            print(f"  {instrument}: failed — {ex}")

    print(f"\n=== Per-pair (cost-adjusted) ===")
    for instrument, df in per_pair.items():
        print_stats(stats(df["net_return"], instrument))

    combined = pd.concat({k: v["net_return"] for k, v in per_pair.items()}, axis=1)
    n_active = combined.notna().sum(axis=1).clip(lower=1)
    portfolio = combined.fillna(0).sum(axis=1) / n_active
    print(f"\n=== PORTFOLIO (7 FX majors, mean-reversion) ===")
    print_stats(stats(portfolio, "PORTFOLIO"))

    resolved = [t for t in all_trades]
    wins = [t for t in resolved if t["pnl_pct"] > 0]
    reverted = [t for t in resolved if t["reason"] == "reverted"]
    timed_out = [t for t in resolved if t["reason"] == "timed_out"]
    print(f"\n=== Trade-level stats across all {len(resolved)} completed trades ===")
    if resolved:
        print(f"win_rate={len(wins)/len(resolved):.3f}  "
              f"reverted={len(reverted)} ({len(reverted)/len(resolved)*100:.0f}%)  "
              f"timed_out={len(timed_out)} ({len(timed_out)/len(resolved)*100:.0f}%)  "
              f"avg_days_held={sum(t['days_held'] for t in resolved)/len(resolved):.1f}")


if __name__ == "__main__":
    main()
