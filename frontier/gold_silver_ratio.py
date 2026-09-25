"""Gold/silver ratio mean-reversion — a classic, well-known commodities
pairs trade: when gold is unusually expensive relative to silver, short
gold / long silver betting on convergence, and the mirror trade when it's
unusually cheap. Genuinely different mechanism from anything else tested —
a real two-leg pairs trade, not a single-instrument position, so each leg
needs its own volatility-targeted sizing (silver is typically far more
volatile than gold in percentage terms — naive equal-dollar sizing would
let silver dominate the P&L) and its own real, asymmetric financing rate
(OANDA CFD financing is not symmetric between long and short — confirmed
2026-09-25: XAU_USD long=-5.62%/yr, short=+3.23%/yr).

Same z-score/SMA20 mean-reversion mechanics as mean_reversion_fx/backtest.py
(entry at 2 std devs, exit on reversion through the mean or after
MAX_HOLD_DAYS), applied to the ratio itself rather than a single
instrument's price.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "trend_system"))

import pandas as pd  # noqa: E402

from backtest import fetch_daily, atr, stats, print_stats, TARGET_DAILY_VOL, MAX_POSITION_WEIGHT  # noqa: E402

SMA_PERIOD = 20
ENTRY_Z = 2.0
EXIT_Z = 0.0
MAX_HOLD_DAYS = 20
STOP_Z = 3.0  # hard stop if the ratio keeps extending past this, added 2026-09-25 after the first run showed
# max_dd=-97% — the March 2020 COVID crash pushed the real ratio to z~4.0 (an all-time record, 124 vs an
# 82 mean/10.5 std), and a 20-day time-based exit alone held through most of that move at up to 4x leverage.
# This exits on further extension, not just on time, before a genuine tail event fully develops.

XAU_SPREAD, XAG_SPREAD = 19.560000, 0.122000
# live OANDA financing rates, 2026-09-25 snapshot (see module docstring)
XAU_LONG_RATE, XAU_SHORT_RATE = -0.0562, 0.0323
XAG_LONG_RATE, XAG_SHORT_RATE = -0.0562, 0.0316


def main() -> None:
    print("Fetching gold + silver daily history...")
    xau = fetch_daily("XAU_USD", count=5000)
    xag = fetch_daily("XAG_USD", count=5000)
    common = xau.index.intersection(xag.index)
    xau, xag = xau.loc[common], xag.loc[common]
    print(f"  {len(common)} common bars, {common.min().date()} to {common.max().date()}")

    ratio = xau["close"] / xag["close"]
    sma = ratio.rolling(SMA_PERIOD).mean()
    std = ratio.rolling(SMA_PERIOD).std()
    z = (ratio - sma) / std.replace(0, pd.NA)

    xau_atr, xag_atr = atr(xau), atr(xag)
    xau_weight = (TARGET_DAILY_VOL / (xau_atr / xau["close"]).clip(lower=1e-6)).clip(upper=MAX_POSITION_WEIGHT)
    xag_weight = (TARGET_DAILY_VOL / (xag_atr / xag["close"]).clip(lower=1e-6)).clip(upper=MAX_POSITION_WEIGHT)

    xau_return = xau["close"].pct_change().fillna(0.0)
    xag_return = xag["close"].pct_change().fillna(0.0)

    net_returns = pd.Series(0.0, index=common)
    trades = []
    position = 0  # 0 flat, +1 = long gold/short silver (bet ratio rises), -1 = short gold/long silver (bet ratio falls)
    days_in_trade = 0
    entry_ratio = None

    idx = common
    for i in range(1, len(idx)):
        today, yesterday = idx[i], idx[i - 1]
        zt = z.get(yesterday)

        if position != 0:
            gold_dir = position   # +1 gold long / -1 gold short
            silver_dir = -position  # opposite side
            gold_rate = XAU_LONG_RATE if gold_dir == 1 else XAU_SHORT_RATE
            silver_rate = XAG_LONG_RATE if silver_dir == 1 else XAG_SHORT_RATE
            price_pnl = (gold_dir * xau_weight.loc[yesterday] * xau_return.loc[today] +
                        silver_dir * xag_weight.loc[yesterday] * xag_return.loc[today])
            financing_pnl = (xau_weight.loc[yesterday] * gold_rate / 365.0 +
                            xag_weight.loc[yesterday] * silver_rate / 365.0)
            net_returns.loc[today] += price_pnl + financing_pnl
            days_in_trade += 1

            reverted = (position == 1 and zt is not None and zt >= EXIT_Z) or \
                       (position == -1 and zt is not None and zt <= EXIT_Z)
            timed_out = days_in_trade >= MAX_HOLD_DAYS
            stopped_out = (position == -1 and zt is not None and zt >= STOP_Z) or \
                          (position == 1 and zt is not None and zt <= -STOP_Z)
            if reverted or timed_out or stopped_out:
                exit_cost = (XAU_SPREAD / xau["close"].loc[today]) * xau_weight.loc[yesterday] + \
                           (XAG_SPREAD / xag["close"].loc[today]) * xag_weight.loc[yesterday]
                net_returns.loc[today] -= exit_cost
                reason = "reverted" if reverted else ("stopped_out" if stopped_out else "timed_out")
                trades.append({"direction": "ratio_up" if position == 1 else "ratio_down",
                               "entry_ratio": entry_ratio, "exit_ratio": ratio.loc[today],
                               "days_held": days_in_trade, "reason": reason})
                position, days_in_trade, entry_ratio = 0, 0, None

        if position == 0 and zt is not None:
            # entry band is (ENTRY_Z, STOP_Z) not entry_z alone — otherwise a
            # just-stopped-out extreme immediately re-triggers the same trade
            # the stop was meant to escape
            new_position = -1 if ENTRY_Z < zt < STOP_Z else (1 if -STOP_Z < zt < -ENTRY_Z else 0)
            if new_position != 0:
                position, days_in_trade, entry_ratio = new_position, 0, ratio.loc[yesterday]
                entry_cost = (XAU_SPREAD / xau["close"].loc[yesterday]) * xau_weight.loc[yesterday] + \
                            (XAG_SPREAD / xag["close"].loc[yesterday]) * xag_weight.loc[yesterday]
                net_returns.loc[today] -= entry_cost

    print(f"\n{len(trades)} completed trades")
    print_stats(stats(net_returns, "Gold/Silver ratio mean-reversion"))

    if trades:
        wins = [t for t in trades if
                (t["direction"] == "ratio_down" and t["exit_ratio"] < t["entry_ratio"]) or
                (t["direction"] == "ratio_up" and t["exit_ratio"] > t["entry_ratio"])]
        reverted = [t for t in trades if t["reason"] == "reverted"]
        stopped = [t for t in trades if t["reason"] == "stopped_out"]
        timed_out = [t for t in trades if t["reason"] == "timed_out"]
        print(f"win_rate={len(wins)/len(trades):.3f}  reverted={len(reverted)} ({len(reverted)/len(trades)*100:.0f}%)  "
              f"stopped_out={len(stopped)} ({len(stopped)/len(trades)*100:.0f}%)  "
              f"timed_out={len(timed_out)} ({len(timed_out)/len(trades)*100:.0f}%)  "
              f"avg_days_held={sum(t['days_held'] for t in trades)/len(trades):.1f}")


if __name__ == "__main__":
    main()
