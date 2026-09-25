"""Prototype: classic multi-asset trend-following, volatility-sized,
long-horizon — a genuinely different strategy family from the FX signal
product (live_scanner.py), built as a separate, side-by-side comparison
per the 2026-09-25 discussion, not a replacement in progress.

Structurally different from everything in fx-signal-model on purpose:
  - Daily bars, not M30/H1/H4 — this is a weeks-to-months holding system,
    not an intraday one. Trade frequency itself was one of the cost
    problems found in the FX project; this sidesteps it by construction.
  - "Always in the market" trend filter (EMA50 vs EMA200 — the classic
    golden-cross/death-cross rule), not a fixed-target bracket trade. The
    exit IS the next crossover, not a stop/target level.
  - Volatility-targeted position sizing (inverse to each instrument's own
    ATR) so every instrument contributes roughly equal risk, rather than
    a flat lot size — this is what lets a diversified multi-asset book
    actually diversify, instead of being dominated by whichever
    instrument happens to be most volatile.
  - Diversified across currencies, metals, and equity indices
    (trend_system/instruments.py) rather than one asset-class family —
    diversification across genuinely uncorrelated markets is the actual
    edge-generation mechanism being tested here, not a cleverer entry
    signal.
  - Real spread cost applied on every position change, from the first
    backtest run — not retrofitted after the fact, which is what actually
    happened on the FX side.

This is explicitly a relative/scale-free comparison (returns as fractions,
not real position sizing or leverage) — same spirit as treating FX signals
in R-multiples rather than claiming real P&L, for the same reason: it's
about whether the underlying edge is real before any conversation about
real capital or real risk limits.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from oandapyV20 import API  # noqa: E402
from oandapyV20.endpoints.instruments import InstrumentsCandles  # noqa: E402
import os  # noqa: E402

from instruments import UNIVERSE  # noqa: E402

FAST_EMA, SLOW_EMA = 50, 200
ATR_PERIOD = 20
TARGET_DAILY_VOL = 0.0075  # ~0.75%/day target risk contribution per instrument — a relative scale, not a leverage claim
MAX_POSITION_WEIGHT = 4.0  # caps sizing on abnormally quiet instruments/periods, avoids absurd implied leverage
HISTORY_BARS = 5000  # OANDA's practical max per request; ~15-18 years of daily bars for the FX majors


def fetch_daily(instrument: str, count: int = HISTORY_BARS) -> pd.DataFrame:
    api_key = os.environ["OANDA_API_KEY"]
    client = API(access_token=api_key, environment=os.environ.get("OANDA_ENVIRONMENT", "practice"))
    resp = client.request(InstrumentsCandles(instrument=instrument,
                                              params={"granularity": "D", "price": "M", "count": count}))
    rows = []
    for candle in resp.get("candles", []):
        if not candle.get("complete"):
            continue
        mid = candle["mid"]
        rows.append({"time": candle["time"], "open": float(mid["o"]), "high": float(mid["h"]),
                     "low": float(mid["l"]), "close": float(mid["c"])})
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time").sort_index()


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def backtest_instrument(instrument: str, spread: float) -> pd.DataFrame:
    """Returns a per-day DataFrame with signal, position_weight, raw_return,
    cost, and net_return — everything needed to both inspect one instrument
    and aggregate into the portfolio."""
    df = fetch_daily(instrument)
    if len(df) < SLOW_EMA + 20:
        return pd.DataFrame()

    close = df["close"]
    ema_fast = close.ewm(span=FAST_EMA, adjust=False).mean()
    ema_slow = close.ewm(span=SLOW_EMA, adjust=False).mean()
    signal = pd.Series(np.where(ema_fast > ema_slow, 1, -1), index=df.index)

    a = atr(df)
    daily_vol_frac = (a / close).clip(lower=1e-6)  # ATR as a fraction of price = instrument's own daily volatility
    position_weight = (TARGET_DAILY_VOL / daily_vol_frac).clip(upper=MAX_POSITION_WEIGHT)

    price_return = close.pct_change().fillna(0.0)
    # yesterday's signal/sizing acts on today's return — no lookahead
    raw_return = signal.shift(1).fillna(0) * position_weight.shift(1).fillna(0) * price_return

    changed = signal != signal.shift(1)
    cost = pd.Series(0.0, index=df.index)
    cost[changed] = (spread / close[changed]) * position_weight.shift(1).fillna(0)[changed]
    net_return = raw_return - cost

    out = pd.DataFrame({
        "close": close, "signal": signal, "position_weight": position_weight,
        "raw_return": raw_return, "cost": cost, "net_return": net_return,
    })
    return out


def stats(returns: pd.Series, label: str) -> dict:
    if returns.empty or returns.std() == 0:
        return {"label": label, "cagr": None, "sharpe": None, "max_dd": None, "total_return": None}
    equity = (1 + returns).cumprod()
    years = len(returns) / 252
    total_return = equity.iloc[-1] - 1
    cagr = equity.iloc[-1] ** (1 / years) - 1 if years > 0 else None
    sharpe = (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() > 0 else None
    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    max_dd = drawdown.min()
    return {"label": label, "total_return": total_return, "cagr": cagr, "sharpe": sharpe, "max_dd": max_dd, "years": years}


def print_stats(s: dict) -> None:
    if s["cagr"] is None:
        print(f"{s['label']:20} insufficient data")
        return
    print(f"{s['label']:20} years={s['years']:.1f} total_return={s['total_return']*100:+.1f}% "
          f"CAGR={s['cagr']*100:+.2f}% sharpe={s['sharpe']:.2f} max_dd={s['max_dd']*100:.1f}%")


def main() -> None:
    per_instrument = {}
    print("Fetching daily history + running per-instrument backtest...")
    for instrument, meta in UNIVERSE.items():
        try:
            df = backtest_instrument(instrument, meta["spread"])
            if df.empty:
                print(f"  {instrument}: insufficient data")
                continue
            per_instrument[instrument] = df
            print(f"  {instrument}: {len(df)} daily bars, {df.index[0].date()} to {df.index[-1].date()}")
        except Exception as ex:
            print(f"  {instrument}: failed — {ex}")

    if not per_instrument:
        print("No instruments produced data — aborting.")
        return

    # Align on common dates, equal-risk-weight across however many instruments have data that day
    all_raw = pd.concat({k: v["raw_return"] for k, v in per_instrument.items()}, axis=1)
    all_net = pd.concat({k: v["net_return"] for k, v in per_instrument.items()}, axis=1)
    n_active = all_raw.notna().sum(axis=1).clip(lower=1)
    portfolio_raw = all_raw.fillna(0).sum(axis=1) / n_active
    portfolio_net = all_net.fillna(0).sum(axis=1) / n_active

    print(f"\n=== Per-instrument (cost-adjusted) ===")
    for instrument, df in per_instrument.items():
        print_stats(stats(df["net_return"], instrument))

    print(f"\n=== PORTFOLIO ({len(per_instrument)} instruments, equal risk-weighted) ===")
    print_stats(stats(portfolio_raw, "raw (no costs)"))
    print_stats(stats(portfolio_net, "cost-adjusted"))

    total_cost_drag = pd.concat({k: v["cost"] for k, v in per_instrument.items()}, axis=1).fillna(0).sum(axis=1).sum()
    print(f"\nTotal cumulative cost drag across all instruments: {total_cost_drag:.4f} (in daily-return-fraction units)")


if __name__ == "__main__":
    main()
