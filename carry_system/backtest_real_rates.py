"""Carry backtest v2 — uses real historical central bank/interbank rate
differentials (fred_rates.py) instead of v1's "today's rate held constant"
approximation. Kept v1 (backtest.py) rather than overwriting it, so the two
are directly comparable — the whole point of doing this properly.

One correctness change beyond just "better data": direction is now DYNAMIC,
following the sign of the actual historical rate differential each day,
not fixed at whatever today's classification happens to be. A real carry
strategy wouldn't stay short GBP for 20 years if GBP's rate differential
flipped positive at some point along the way — v1 could only assume a
static direction because it only had one (today's) data point. Cost is
charged on every direction flip, not just the initial entry, for the same
reason: rebalancing when the real world differential reverses is a real,
costed event, not a one-off.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "trend_system"))
# trend_system inserted last (resolved first) — carry_system has its own
# backtest.py (v1) which would otherwise shadow trend_system/backtest.py
# under the same import name, same issue hit in mean_reversion_fx

import pandas as pd  # noqa: E402

from backtest import fetch_daily, atr, stats, print_stats, TARGET_DAILY_VOL, MAX_POSITION_WEIGHT  # noqa: E402
from fred_rates import load_all_rates, pair_rate_diff  # noqa: E402

PAIRS = [  # (oanda_instrument, base_ccy, quote_ccy, spread)
    ("EUR_USD", "EUR", "USD", 0.000370), ("GBP_USD", "GBP", "USD", 0.000750),
    ("USD_JPY", "USD", "JPY", 0.057000), ("USD_CHF", "USD", "CHF", 0.000920),
    ("AUD_USD", "AUD", "USD", 0.001130), ("USD_CAD", "USD", "CAD", 0.000990),
    ("NZD_USD", "NZD", "USD", 0.001590), ("AUD_JPY", "AUD", "JPY", 0.030000),
    ("NZD_JPY", "NZD", "JPY", 0.035000), ("AUD_CHF", "AUD", "CHF", 0.001800),
    ("NZD_CHF", "NZD", "CHF", 0.001280), ("GBP_JPY", "GBP", "JPY", 0.222000),
    ("EUR_JPY", "EUR", "JPY", 0.152000),
]


def backtest_pair(instrument: str, base: str, quote: str, spread: float, rates: dict) -> pd.DataFrame:
    df = fetch_daily(instrument)
    if len(df) < 250:
        return pd.DataFrame()
    close = df["close"]
    a = atr(df)
    daily_vol_frac = (a / close).clip(lower=1e-6)
    position_weight = (TARGET_DAILY_VOL / daily_vol_frac).clip(upper=MAX_POSITION_WEIGHT)
    price_return = close.pct_change().fillna(0.0)

    rate_diff = pair_rate_diff(rates, base, quote)  # real, time-varying, daily
    rate_diff = rate_diff.reindex(df.index, method="ffill")
    direction = rate_diff.apply(lambda x: 1 if x >= 0 else -1)

    price_only_raw = direction.shift(1).fillna(0) * position_weight.shift(1).fillna(0) * price_return
    financing_daily = rate_diff.abs() / 365.0  # always the positive side, since direction follows the sign
    price_plus_financing_raw = price_only_raw + position_weight.shift(1).fillna(0) * financing_daily.shift(1).fillna(0)

    flipped = direction != direction.shift(1)
    cost = pd.Series(0.0, index=df.index)
    cost[flipped] = (spread / close[flipped]) * position_weight.shift(1).fillna(0)[flipped]

    return pd.DataFrame({
        "close": close, "direction": direction, "rate_diff_pct": rate_diff * 100,
        "price_only_net": price_only_raw - cost,
        "price_plus_financing_net": price_plus_financing_raw - cost,
        "flips": flipped,
    })


def main() -> None:
    print("Loading real historical rate history (FRED)...")
    rates = load_all_rates()
    for ccy, s in rates.items():
        print(f"  {ccy}: {s.index.min().date()} to {s.index.max().date()}")

    per_pair = {}
    print("\nBacktesting each pair against real point-in-time rate differentials...")
    for instrument, base, quote, spread in PAIRS:
        try:
            df = backtest_pair(instrument, base, quote, spread, rates)
            if df.empty:
                print(f"  {instrument}: insufficient data")
                continue
            per_pair[instrument] = df
            n_flips = int(df["flips"].sum())
            print(f"  {instrument}: {len(df)} bars, {n_flips} direction flips over the period")
        except Exception as ex:
            print(f"  {instrument}: failed — {ex}")

    print(f"\n=== Per-pair, PRICE-ONLY (real rates, dynamic direction) ===")
    for instrument, df in per_pair.items():
        print_stats(stats(df["price_only_net"], instrument))

    print(f"\n=== Per-pair, PRICE + real financing (real rates, dynamic direction) ===")
    for instrument, df in per_pair.items():
        print_stats(stats(df["price_plus_financing_net"], instrument))

    for label, key in [("PRICE-ONLY", "price_only_net"), ("PRICE + real financing", "price_plus_financing_net")]:
        combined = pd.concat({k: v[key] for k, v in per_pair.items()}, axis=1)
        n_active = combined.notna().sum(axis=1).clip(lower=1)
        portfolio = combined.fillna(0).sum(axis=1) / n_active
        print(f"\n=== PORTFOLIO, {label} ===")
        print_stats(stats(portfolio, f"PORTFOLIO ({label})"))


if __name__ == "__main__":
    main()
