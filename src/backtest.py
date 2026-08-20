"""Simple vectorised backtester for the technical layer (PESTLE is a daily
overlay, not backtestable on synthetic intraday data without real news
history — see MODEL_SPEC.md §7 for the phased validation plan).

Trade rule: on a triggered tech_score signal, enter next bar's open, exit at
ATR-based SL/TP (from combiner.py) or after `max_hold_bars`, whichever first.
This is a simplification for a first-pass sanity check, not the final
execution model.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import pandas as pd
from strategies.composite import technical_score
from data.dukascopy import get_ohlc


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    win_rate: float
    expectancy_r: float
    max_drawdown_r: float
    total_trades: int


def run_backtest(df: pd.DataFrame, entry_threshold: float = 0.35, max_hold_bars: int = 12,
                  rr: float = 1.5, weights: dict | None = None) -> BacktestResult:
    scored = technical_score(df, weights=weights)
    trades = []
    i = 0
    n = len(df)
    while i < n - 1:
        score = scored["tech_score"].iloc[i]
        if abs(score) < entry_threshold:
            i += 1
            continue
        direction = 1 if score > 0 else -1
        entry_idx = i + 1
        if entry_idx >= n:
            break
        entry_price = df["open"].iloc[entry_idx]
        a = scored["atr"].iloc[i]
        if pd.isna(a) or a == 0:
            i += 1
            continue
        stop = entry_price - direction * 1.0 * a
        target = entry_price + direction * rr * a

        outcome, exit_price, hold = None, None, 0
        for j in range(entry_idx, min(entry_idx + max_hold_bars, n)):
            bar = df.iloc[j]
            hit_stop = (bar["low"] <= stop) if direction == 1 else (bar["high"] >= stop)
            hit_target = (bar["high"] >= target) if direction == 1 else (bar["low"] <= target)
            hold = j - entry_idx
            if hit_stop and hit_target:
                outcome, exit_price = "stop", stop  # conservative: assume stop hit first if both in-bar
                break
            if hit_stop:
                outcome, exit_price = "stop", stop
                break
            if hit_target:
                outcome, exit_price = "target", target
                break
        if outcome is None:
            exit_price = df["close"].iloc[min(entry_idx + max_hold_bars, n) - 1]
            outcome = "timeout"

        r_multiple = direction * (exit_price - entry_price) / a
        trades.append({
            "entry_time": df.index[entry_idx], "direction": "long" if direction == 1 else "short",
            "entry": entry_price, "exit": exit_price, "outcome": outcome,
            "r_multiple": r_multiple, "hold_bars": hold, "tech_score": score,
        })
        i = entry_idx + max(hold, 1)

    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        return BacktestResult(trades_df, 0.0, 0.0, 0.0, 0)

    win_rate = (trades_df["r_multiple"] > 0).mean()
    expectancy = trades_df["r_multiple"].mean()
    cum = trades_df["r_multiple"].cumsum()
    max_dd = (cum.cummax() - cum).max()

    return BacktestResult(trades_df, win_rate, expectancy, max_dd, len(trades_df))


def synthetic_ohlc(n_bars: int = 2000, start="2026-06-01", freq="30min", seed: int = 7) -> pd.DataFrame:
    """Generates plausible-looking synthetic 30-min FX OHLC for smoke-testing
    the pipeline. NOT a substitute for real historical data — see
    MODEL_SPEC.md §7: real backtests need real broker/vendor OHLC history."""
    import numpy as np
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start=start, periods=n_bars, freq=freq, tz="UTC")
    returns = rng.normal(0, 0.0006, n_bars)
    # inject some trending/breakout structure so strategies have something to find
    drift = np.sin(np.arange(n_bars) / 48) * 0.0004
    price = 1.2700 * np.exp(np.cumsum(returns + drift))
    open_ = price
    close = price * (1 + rng.normal(0, 0.0003, n_bars))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0004, n_bars)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0004, n_bars)))
    vol = rng.integers(100, 1000, n_bars)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


def load_real_ohlc(symbol: str, start: datetime, end: datetime, freq: str = "30min", use_cache: bool = True) -> pd.DataFrame:
    """Real Dukascopy OHLC for backtesting. See data/dukascopy.py — network
    access to datafeed.dukascopy.com wasn't reachable from the build sandbox,
    so run this from your own machine to actually pull data; the parser
    itself is unit-tested in tests/test_dukascopy.py."""
    return get_ohlc(symbol, start, end, freq=freq, use_cache=use_cache)


def run_backtest_on_real_data(symbol: str, start: datetime, end: datetime, freq: str = "30min",
                               use_cache: bool = True, **kwargs) -> BacktestResult:
    df = load_real_ohlc(symbol, start, end, freq=freq, use_cache=use_cache)
    if df.empty or len(df) < 100:
        raise ValueError(
            f"Got {len(df)} bars for {symbol} {start}–{end}. Check dates are within "
            "Dukascopy's history, the symbol format (e.g. EURUSD), and that this "
            "machine can reach datafeed.dukascopy.com."
        )
    return run_backtest(df, **kwargs)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Backtest the technical composite on real Dukascopy data.")
    parser.add_argument("symbol", nargs="?", default="EURUSD")
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-02-01")
    parser.add_argument("--freq", default="30min")
    args = parser.parse_args()

    start_dt = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    result = run_backtest_on_real_data(args.symbol, start_dt, end_dt, freq=args.freq)
    print(f"{args.symbol} {args.start} to {args.end} ({args.freq} bars)")
    print(f"Trades: {result.total_trades}")
    print(f"Win rate: {result.win_rate:.1%}")
    print(f"Expectancy: {result.expectancy_r:+.2f}R per trade")
    print(f"Max drawdown: {result.max_drawdown_r:.2f}R")
