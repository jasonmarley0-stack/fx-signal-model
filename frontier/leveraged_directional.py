"""High-risk directional sleeve, built with explicit guiding principles
rather than gut-feel — 2026-09-25 discussion: a bounded, rules-based
monthly stake, not a lump-sum gamble.

Rules, stated plainly, not implied:
  1. Signal: the same EMA50/EMA200 trend crossover already validated
     elsewhere in this project (trend_system/backtest.py) — a real,
     understood signal, not a new unproven one.
  2. Fresh £250-equivalent stake every calendar month. Ends above £250 ->
     excess is swept out (modeled as "banked", not reinvested here). Ends
     below -> topped back up to £250 for the next month. This sleeve
     never compounds internally — all upside routes to the safe,
     already-validated strategy, exactly as specified.
  3. Position size is DERIVED from a fixed risk budget (RISK_PER_TRADE_PCT
     of the CURRENT stake, which shrinks after a loss) and the stop
     distance — leverage is a consequence of that, not a chosen number.
     This is what gives "several attempts" instead of one all-or-nothing
     bet, and it's what makes this different from just picking a big
     leverage number and hoping.
  4. Hard stop at the risk-budgeted distance. Exit on trend reversal
     otherwise (ride winners, don't cap them with an early fixed target).

None of this manufactures edge — it controls what a losing streak costs
and lets whatever real edge the EMA signal has show up cleanly. The
backtest below is the honest test of whether that edge is real on
crypto specifically (it's only been tested on FX/metals/equities so far).
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "trend_system"))

import time  # noqa: E402
import requests  # noqa: E402
import pandas as pd  # noqa: E402
import numpy as np  # noqa: E402
from backtest import atr, FAST_EMA, SLOW_EMA  # noqa: E402

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
MONTHLY_STAKE = 250.0  # GBP-equivalent, modeled in USD terms for simplicity — real FX conversion is a separate, minor operational detail
RISK_PER_TRADE_PCT = 0.25  # 25% of the CURRENT (not original) stake risked per trade attempt
STOP_ATR_MULT = 1.5  # same convention as the rest of this project's stop sizing
MAX_LEVERAGE = 20.0  # cap even when the ATR-derived stop distance is very tight, to avoid a position so thin it liquidates on noise
MAX_ATTEMPTS_PER_MONTH = 5  # cap on sequential re-entries within one month, avoids unbounded looping in extreme chop
ROUND_TRIP_COST_PCT = 0.0024  # same REALISTIC crypto fee assumption as the funding-harvest work


def fetch_daily_crypto(symbol: str) -> pd.DataFrame:
    """Fetches real daily OHLC (not just close) — ATR needs the actual
    high/low range, not a close-to-close proxy, which would understate
    volatility and make stops artificially tight."""
    all_rows = []
    start_time = 1483228800000
    while True:
        params = {"symbol": symbol, "interval": "1d", "limit": 1000, "startTime": start_time}
        resp = requests.get("https://api.binance.com/api/v3/klines", params=params, timeout=30)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < 1000:
            break
        start_time = rows[-1][0] + 1
        time.sleep(0.2)
    df = pd.DataFrame({
        "time": [pd.to_datetime(r[0], unit="ms", utc=True) for r in all_rows],
        "high": [float(r[2]) for r in all_rows],
        "low": [float(r[3]) for r in all_rows],
        "close": [float(r[4]) for r in all_rows],
    }).set_index("time").sort_index()
    df.index = df.index.tz_localize(None)  # match close.index usage elsewhere (naive daily timestamps)
    return df


def simulate_symbol(symbol: str) -> tuple[pd.DataFrame, list[dict]]:
    df = fetch_daily_crypto(symbol)
    close = df["close"]
    if len(close) < SLOW_EMA + 30:
        return pd.DataFrame(), []

    ema_fast = close.ewm(span=FAST_EMA, adjust=False).mean()
    ema_slow = close.ewm(span=SLOW_EMA, adjust=False).mean()
    trend = pd.Series(np.where(ema_fast > ema_slow, 1, -1), index=close.index)
    # entries decide off YESTERDAY's confirmed trend (no lookahead), matching
    # trend_system/backtest.py's signal.shift(1) convention — exits still check
    # TODAY's trend, which is correct (checking current status isn't lookahead)
    trend_for_entry = trend.shift(1)
    a = atr(df)  # real high/low range, not a close-only proxy

    months = pd.period_range(close.index.min(), close.index.max(), freq="M")
    monthly_results = []
    trade_log = []

    for month in months:
        month_days = close.index[(close.index.to_period("M") == month)]
        if len(month_days) < 2:
            continue
        stake = MONTHLY_STAKE
        attempts = 0
        i = 0
        day_list = list(month_days)
        position = None  # dict: direction, entry_price, stop_price, notional

        while i < len(day_list) - 1:
            today = day_list[i]
            if position is None:
                if (attempts >= MAX_ATTEMPTS_PER_MONTH or pd.isna(a.get(today)) or a.get(today, 0) <= 0
                        or pd.isna(trend_for_entry.get(today))):
                    i += 1
                    continue
                direction = int(trend_for_entry.loc[today])
                stop_distance = STOP_ATR_MULT * a.loc[today]
                entry_price = close.loc[today]
                stop_price = entry_price - direction * stop_distance
                risk_amount = RISK_PER_TRADE_PCT * stake
                stop_frac = stop_distance / entry_price
                notional = risk_amount / stop_frac
                implied_leverage = min(notional / max(stake, 1e-9), MAX_LEVERAGE)
                notional = implied_leverage * stake
                entry_cost = notional * ROUND_TRIP_COST_PCT / 2  # half now, half on exit
                stake -= entry_cost
                position = {"direction": direction, "entry_price": entry_price, "stop_price": stop_price,
                           "notional": notional, "entry_day": today}
                attempts += 1
                i += 1
                continue

            price_today = close.loc[today]
            hit_stop = (price_today <= position["stop_price"]) if position["direction"] == 1 else \
                       (price_today >= position["stop_price"])
            direction_today = int(trend.loc[today])
            reversed_ = direction_today != position["direction"]

            if hit_stop or reversed_:
                exit_price = position["stop_price"] if hit_stop else price_today
                pnl_frac = position["direction"] * (exit_price - position["entry_price"]) / position["entry_price"]
                pnl = pnl_frac * position["notional"]
                exit_cost = position["notional"] * ROUND_TRIP_COST_PCT / 2
                stake += pnl - exit_cost
                stake = max(stake, 0.0)
                trade_log.append({"month": str(month), "symbol": symbol, "direction": position["direction"],
                                  "entry": position["entry_price"], "exit": exit_price,
                                  "pnl": pnl, "reason": "stopped" if hit_stop else "trend_reversed",
                                  "leverage": position["notional"] / MONTHLY_STAKE})
                position = None
            i += 1

        if position is not None:  # force-close at month end
            exit_price = close.loc[day_list[-1]]
            pnl_frac = position["direction"] * (exit_price - position["entry_price"]) / position["entry_price"]
            pnl = pnl_frac * position["notional"]
            exit_cost = position["notional"] * ROUND_TRIP_COST_PCT / 2
            stake += pnl - exit_cost
            stake = max(stake, 0.0)
            trade_log.append({"month": str(month), "symbol": symbol, "direction": position["direction"],
                              "entry": position["entry_price"], "exit": exit_price,
                              "pnl": pnl, "reason": "month_end", "leverage": position["notional"] / MONTHLY_STAKE})

        banked = max(0.0, stake - MONTHLY_STAKE)
        monthly_results.append({"month": str(month), "symbol": symbol, "ending_stake": stake,
                                "banked": banked, "attempts": attempts})

    return pd.DataFrame(monthly_results), trade_log


def main() -> None:
    all_monthly = []
    all_trades = []
    for symbol in SYMBOLS:
        print(f"Simulating {symbol}...")
        monthly, trades = simulate_symbol(symbol)
        if monthly.empty:
            print(f"  insufficient data")
            continue
        all_monthly.append(monthly)
        all_trades.extend(trades)
        print(f"  {len(monthly)} months simulated, {len(trades)} trades taken")

    combined = pd.concat(all_monthly, ignore_index=True)
    for symbol in SYMBOLS:
        sub = combined[combined["symbol"] == symbol]
        if sub.empty:
            continue
        wins = sub[sub["ending_stake"] > MONTHLY_STAKE]
        wipeouts = sub[sub["ending_stake"] < MONTHLY_STAKE * 0.1]
        print(f"\n=== {symbol}: {len(sub)} months ===")
        print(f"  months ending above £250 (a 'win' month): {len(wins)} ({len(wins)/len(sub)*100:.0f}%)")
        print(f"  months ending near-total-loss (<10% of stake left): {len(wipeouts)} ({len(wipeouts)/len(sub)*100:.0f}%)")
        print(f"  median ending stake: £{sub['ending_stake'].median():.0f}")
        print(f"  mean ending stake: £{sub['ending_stake'].mean():.0f}")
        print(f"  best month: £{sub['ending_stake'].max():.0f}   worst month: £{sub['ending_stake'].min():.0f}")
        total_banked = sub["banked"].sum()
        total_contributed = len(sub) * MONTHLY_STAKE
        print(f"  total banked over {len(sub)} months: £{total_banked:.0f} vs £{total_contributed:.0f} contributed "
              f"({total_banked/total_contributed*100:+.1f}% of contributed capital)")

    all_trades_df = pd.DataFrame(all_trades)
    if not all_trades_df.empty:
        wins = all_trades_df[all_trades_df["pnl"] > 0]
        print(f"\n=== Trade-level, all symbols combined ({len(all_trades_df)} trades) ===")
        print(f"win_rate={len(wins)/len(all_trades_df):.3f}  avg_leverage_used={all_trades_df['leverage'].mean():.1f}x  "
              f"avg_win=£{wins['pnl'].mean():.1f}  avg_loss=£{all_trades_df[all_trades_df['pnl']<=0]['pnl'].mean():.1f}")


if __name__ == "__main__":
    main()
