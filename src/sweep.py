"""Multi-pair, multi-period, per-strategy backtest sweep.

Runs the technical backtest across every combination of (pair, period,
strategy variant) so we can see:
  1. Whether any edge shown on one pair/month generalizes to others.
  2. Whether ORB, trend-following, or candlestick patterns individually
     carry an edge that the blended composite might be diluting.

Usage:
    cd src
    python3 sweep.py                          # defaults: 7 majors x 3 months x 4 variants
    python3 sweep.py --pairs EURUSD,GBPUSD --months 2026-01,2026-02
    python3 sweep.py --out my_results.csv

Output: a CSV with one row per (pair, period, variant) and a printed summary
table aggregated by variant (mean win rate / expectancy / trade count across
all pairs and periods) so you can see at a glance whether any single
sub-strategy stands out.
"""
from __future__ import annotations
import argparse
import calendar
from datetime import datetime, timezone
import pandas as pd

from backtest import run_backtest_on_real_data, BacktestResult

DEFAULT_PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
DEFAULT_MONTHS = ["2025-11", "2025-12", "2026-01"]

VARIANTS = {
    "orb_only": {"orb": 1.0, "trend": 0.0, "pattern": 0.0},
    "trend_only": {"orb": 0.0, "trend": 1.0, "pattern": 0.0},
    "pattern_only": {"orb": 0.0, "trend": 0.0, "pattern": 1.0},
    "composite_default": {"orb": 0.5, "trend": 0.3, "pattern": 0.2},
}


def month_bounds(month_str: str) -> tuple[datetime, datetime]:
    """'2026-01' -> (2026-01-01 00:00 UTC, 2026-02-01 00:00 UTC)"""
    year, month = (int(x) for x in month_str.split("-"))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    days_in_month = calendar.monthrange(year, month)[1]
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, end


def run_sweep(pairs: list[str], months: list[str], freq: str = "30min",
              use_cache: bool = True, verbose: bool = True) -> pd.DataFrame:
    rows = []
    total = len(pairs) * len(months) * len(VARIANTS)
    done = 0
    for pair in pairs:
        for month in months:
            start, end = month_bounds(month)
            for variant_name, weights in VARIANTS.items():
                done += 1
                if verbose:
                    print(f"[{done}/{total}] {pair} {month} {variant_name}...")
                try:
                    result: BacktestResult = run_backtest_on_real_data(
                        pair, start, end, freq=freq, use_cache=use_cache, weights=weights,
                    )
                    rows.append({
                        "pair": pair, "month": month, "variant": variant_name,
                        "trades": result.total_trades, "win_rate": result.win_rate,
                        "expectancy_r": result.expectancy_r, "max_drawdown_r": result.max_drawdown_r,
                        "error": None,
                    })
                except Exception as exc:  # noqa: BLE001 — a single failed slice shouldn't kill the sweep
                    rows.append({
                        "pair": pair, "month": month, "variant": variant_name,
                        "trades": 0, "win_rate": None, "expectancy_r": None, "max_drawdown_r": None,
                        "error": str(exc),
                    })
                    if verbose:
                        print(f"    FAILED: {exc}")
    return pd.DataFrame(rows)


def summarize_by_variant(results: pd.DataFrame) -> pd.DataFrame:
    ok = results[results["error"].isna() & (results["trades"] > 0)]
    summary = ok.groupby("variant").agg(
        total_trades=("trades", "sum"),
        avg_win_rate=("win_rate", "mean"),
        avg_expectancy_r=("expectancy_r", "mean"),
        avg_max_drawdown_r=("max_drawdown_r", "mean"),
        slices_run=("trades", "count"),
    ).round(3)
    return summary.sort_values("avg_expectancy_r", ascending=False)


def summarize_by_pair(results: pd.DataFrame) -> pd.DataFrame:
    ok = results[results["error"].isna() & (results["trades"] > 0)]
    summary = ok.groupby(["pair", "variant"]).agg(
        trades=("trades", "sum"),
        avg_win_rate=("win_rate", "mean"),
        avg_expectancy_r=("expectancy_r", "mean"),
    ).round(3)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sweep the technical strategies across pairs/months/variants.")
    parser.add_argument("--pairs", default=",".join(DEFAULT_PAIRS), help="Comma-separated pairs, e.g. EURUSD,GBPUSD")
    parser.add_argument("--months", default=",".join(DEFAULT_MONTHS), help="Comma-separated YYYY-MM, e.g. 2026-01,2026-02")
    parser.add_argument("--freq", default="30min")
    parser.add_argument("--out", default="sweep_results.csv")
    parser.add_argument("--no-cache", action="store_true", help="Force re-download instead of using data_cache/")
    args = parser.parse_args()

    pairs = [p.strip() for p in args.pairs.split(",")]
    months = [m.strip() for m in args.months.split(",")]

    print(f"Sweeping {len(pairs)} pairs x {len(months)} months x {len(VARIANTS)} variants "
          f"({len(pairs) * len(months) * len(VARIANTS)} backtests total)...")
    print("This downloads real tick data for every pair/month not already cached — expect this")
    print("to take a while on the first run; re-runs reuse data_cache/ and are much faster.\n")

    results = run_sweep(pairs, months, freq=args.freq, use_cache=not args.no_cache)
    results.to_csv(args.out, index=False)
    print(f"\nWrote {len(results)} rows to {args.out}")

    n_errors = results["error"].notna().sum()
    if n_errors:
        print(f"\n{n_errors} slice(s) failed (see '{args.out}' error column for details) — "
              f"most likely a stretch of missing Dukascopy data for that pair/month.")

    print("\n=== Summary by strategy variant (averaged across all pairs/months) ===")
    print(summarize_by_variant(results).to_string())

    print("\n=== Detail by pair x variant ===")
    print(summarize_by_pair(results).to_string())

    print("\nReminder: positive average expectancy here is a starting hypothesis to keep testing,")
    print("not a green light to trade — see MODEL_SPEC.md §7 and RUNNING_REAL_BACKTEST.md §6.")
