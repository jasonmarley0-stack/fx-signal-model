"""70/30 split simulation: £175/month into the validated crypto funding-
harvest strategy (compounding), £75/month into the bounded leveraged-
directional sleeve (never compounds internally — any month's profit above
its stake is swept into the compounding side, exactly as specified
2026-09-25). Reuses the real, already-validated per-period return series
from both backtests, aligned by actual calendar month, not an abstract
approximation — and reports a 100%-into-the-safe-strategy baseline
alongside it for a direct, honest comparison.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd  # noqa: E402

from crypto_funding import fetch_all_funding, COST_VARIANTS  # noqa: E402
from crypto_funding_with_basis import fetch_klines, walk_with_basis  # noqa: E402
import leveraged_directional as ld  # noqa: E402

TOTAL_MONTHLY = 250.0
SAFE_SHARE, RISKY_SHARE = 0.70, 0.30
SAFE_MONTHLY_CONTRIB = TOTAL_MONTHLY * SAFE_SHARE  # £175
RISKY_MONTHLY_STAKE = TOTAL_MONTHLY * RISKY_SHARE  # £75, split across BTC+ETH below
SYMBOLS = ["BTCUSDT", "ETHUSDT"]


def funding_harvest_monthly_returns() -> pd.Series:
    """Real, REALISTIC-cost, basis-risk-inclusive per-period returns for
    the BTC+ETH portfolio (same construction as crypto_funding_with_basis.py's
    main()), resampled to monthly by compounding within each month."""
    cost = COST_VARIANTS["REALISTIC (spot taker + futures maker)"]
    per_symbol = {}
    for symbol in SYMBOLS:
        funding = fetch_all_funding(symbol)
        funding.index = funding.index.floor("s")
        spot_close = fetch_klines("https://api.binance.com/api/v3/klines", symbol, 1483228800000)
        perp_close = fetch_klines("https://fapi.binance.com/fapi/v1/klines", symbol, 1483228800000)
        spot_return = spot_close.pct_change().fillna(0.0)
        perp_return = perp_close.pct_change().fillna(0.0)
        common = funding.index.intersection(spot_return.index).intersection(perp_return.index)
        net = walk_with_basis(funding.loc[common], spot_return.loc[common], perp_return.loc[common], cost)
        per_symbol[symbol] = net

    combined = pd.concat(per_symbol, axis=1)
    n_active = combined.notna().sum(axis=1).clip(lower=1)
    portfolio = combined.fillna(0).sum(axis=1) / n_active
    monthly = (1 + portfolio).resample("MS").prod() - 1
    monthly.index = monthly.index.to_period("M").astype(str)
    return monthly


def directional_sleeve_monthly_banked() -> pd.Series:
    """Real monthly banked amounts from the leveraged-directional sleeve,
    BTC+ETH combined, each run at half the risky stake (£37.50 each)."""
    ld.MONTHLY_STAKE = RISKY_MONTHLY_STAKE / len(SYMBOLS)
    banked_by_month: dict[str, float] = {}
    for symbol in SYMBOLS:
        monthly, _ = ld.simulate_symbol(symbol)
        if monthly.empty:
            continue
        for _, row in monthly.iterrows():
            banked_by_month[row["month"]] = banked_by_month.get(row["month"], 0.0) + row["banked"]
    return pd.Series(banked_by_month).sort_index()


def main() -> None:
    print("Building real, calendar-aligned monthly return series for both strategies...")
    safe_returns = funding_harvest_monthly_returns()
    print(f"  funding-harvest: {len(safe_returns)} real months, {safe_returns.index.min()} to {safe_returns.index.max()}")
    risky_banked = directional_sleeve_monthly_banked()
    print(f"  directional sleeve: {len(risky_banked)} real months, {risky_banked.index.min()} to {risky_banked.index.max()}")

    common_months = sorted(set(safe_returns.index) & set(risky_banked.index))
    print(f"  {len(common_months)} months with both series present\n")

    balance_70_30 = 0.0
    balance_baseline = 0.0
    total_banked = 0.0
    history = []
    for month in common_months:
        r = safe_returns.loc[month]
        banked = risky_banked.loc[month]
        total_banked += banked

        balance_70_30 = balance_70_30 * (1 + r) + SAFE_MONTHLY_CONTRIB + banked
        balance_baseline = balance_baseline * (1 + r) + TOTAL_MONTHLY

        history.append({"month": month, "balance_70_30": balance_70_30, "balance_baseline": balance_baseline})

    n_months = len(common_months)
    total_contributed = n_months * TOTAL_MONTHLY
    print(f"=== Over {n_months} real months ({n_months/12:.1f} years), £{TOTAL_MONTHLY:.0f}/month contributed ===\n")
    print(f"Total contributed: £{total_contributed:,.0f}")
    print(f"Total banked from directional sleeve (included in 70/30 balance below): £{total_banked:,.0f}")
    print(f"\n70/30 split final balance:      £{balance_70_30:,.0f}  ({(balance_70_30/total_contributed-1)*100:+.1f}% vs contributed)")
    print(f"100% safe-strategy final balance: £{balance_baseline:,.0f}  ({(balance_baseline/total_contributed-1)*100:+.1f}% vs contributed)")
    print(f"\nDifference: £{balance_70_30 - balance_baseline:,.0f} ({'70/30 ahead' if balance_70_30 > balance_baseline else '100% safe ahead'})")

    hist_df = pd.DataFrame(history)
    print("\n--- every 12th month, for a sense of the path ---")
    print(hist_df.iloc[::12][["month", "balance_70_30", "balance_baseline"]].to_string(index=False))


if __name__ == "__main__":
    main()
