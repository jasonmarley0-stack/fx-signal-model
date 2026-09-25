"""Carry + momentum filter — the documented fix for carry's actual known
weakness (crash risk: carry blows up when the funding currency suddenly
rallies hard against you, e.g. CHF in Jan 2015). Standard construction:
only hold full carry size when price momentum agrees with the carry
direction; reduce or flatten when momentum strongly disagrees. Tests two
variants of "disagreement" response — DAMPEN (reduce to 30% size, keep
collecting some financing) vs FLATTEN (fully exit, no financing while
momentum disagrees) — rather than assuming which is better.

Built on backtest_real_rates.py's real point-in-time rate differentials,
restricted to the 5 JPY-crosses already validated as the strongest,
most stable subset (backtest_jpy_crosses.py: CAGR +2.80%, Sharpe 0.44).
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "trend_system"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from backtest import fetch_daily, atr, stats, print_stats, TARGET_DAILY_VOL, MAX_POSITION_WEIGHT  # noqa: E402
from fred_rates import load_all_rates, pair_rate_diff  # noqa: E402

PAIRS = [  # (oanda_instrument, base_ccy, quote_ccy, spread)
    ("USD_JPY", "USD", "JPY", 0.057000), ("AUD_JPY", "AUD", "JPY", 0.030000),
    ("NZD_JPY", "NZD", "JPY", 0.035000), ("GBP_JPY", "GBP", "JPY", 0.222000),
    ("EUR_JPY", "EUR", "JPY", 0.152000),
]
MOMENTUM_FAST, MOMENTUM_SLOW = 50, 200
DAMPEN_FACTOR = 0.3


def backtest_pair(instrument: str, base: str, quote: str, spread: float, rates: dict, mode: str) -> pd.DataFrame:
    """mode: 'none' (pure carry, no filter), 'dampen', or 'flatten'."""
    df = fetch_daily(instrument)
    if len(df) < 250:
        return pd.DataFrame()
    close = df["close"]
    a = atr(df)
    daily_vol_frac = (a / close).clip(lower=1e-6)
    base_weight = (TARGET_DAILY_VOL / daily_vol_frac).clip(upper=MAX_POSITION_WEIGHT)
    price_return = close.pct_change().fillna(0.0)

    rate_diff = pair_rate_diff(rates, base, quote).reindex(df.index, method="ffill")
    carry_direction = rate_diff.apply(lambda x: 1 if x >= 0 else -1)

    ema_fast = close.ewm(span=MOMENTUM_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MOMENTUM_SLOW, adjust=False).mean()
    momentum_direction = pd.Series(np.where(ema_fast > ema_slow, 1, -1), index=df.index)

    agrees = (carry_direction == momentum_direction)
    if mode == "none":
        weight_multiplier = pd.Series(1.0, index=df.index)
    elif mode == "dampen":
        weight_multiplier = agrees.apply(lambda a: 1.0 if a else DAMPEN_FACTOR)
    elif mode == "flatten":
        weight_multiplier = agrees.apply(lambda a: 1.0 if a else 0.0)
    else:
        raise ValueError(mode)

    position_weight = base_weight * weight_multiplier
    price_pnl = carry_direction.shift(1).fillna(0) * position_weight.shift(1).fillna(0) * price_return
    financing_pnl = position_weight.shift(1).fillna(0) * (rate_diff.abs() / 365.0).shift(1).fillna(0)

    effective_direction = carry_direction * (weight_multiplier > 0).astype(int)
    flipped = effective_direction != effective_direction.shift(1)
    cost = pd.Series(0.0, index=df.index)
    cost[flipped] = (spread / close[flipped]) * position_weight.shift(1).fillna(0)[flipped]

    net_return = price_pnl + financing_pnl - cost
    return pd.DataFrame({"close": close, "net_return": net_return})


def main() -> None:
    print("Loading real historical rate history (FRED)...")
    rates = load_all_rates()

    for mode in ("none", "dampen", "flatten"):
        per_pair = {}
        for instrument, base, quote, spread in PAIRS:
            df = backtest_pair(instrument, base, quote, spread, rates, mode)
            if not df.empty:
                per_pair[instrument] = df
        combined = pd.concat({k: v["net_return"] for k, v in per_pair.items()}, axis=1)
        n_active = combined.notna().sum(axis=1).clip(lower=1)
        portfolio = combined.fillna(0).sum(axis=1) / n_active
        print(f"\n=== mode={mode} ===")
        for instrument, df in per_pair.items():
            print_stats(stats(df["net_return"], f"  {instrument}"))
        print_stats(stats(portfolio, f"PORTFOLIO (mode={mode})"))


if __name__ == "__main__":
    main()
