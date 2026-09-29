"""Candidate 3: trend-pullback continuation, an H4 strategy structurally
different from the weighted ORB/trend/pattern composite -- a single,
fixed rule set, documented here and chosen before any evaluation. No
parameter sweep, no PESTLE, no confidence tiers (there is no internal
score magnitude to tier by, so every fired signal uses one fixed
confidence label).

Rules (fixed, not tuned for this milestone):
  - Trend: EMA20 vs EMA50 on H4 closes (same EMA pair
    strategies/trend_following.py already uses -- reused for a directly
    comparable trend definition, not because it was optimised here).
    Uptrend when EMA20 > EMA50, downtrend when EMA20 < EMA50.
  - Pullback + resumption (long): the PREVIOUS H4 bar's low touched or
    crossed below EMA20 (a genuine pullback into the fast EMA) AND the
    CURRENT bar closes back above EMA20 with a higher close than the
    previous bar (confirms resumption, not just a touch). Mirrored for a
    downtrend/short.
  - Fires once per fresh signal (not on every subsequent bar while the
    condition happens to keep re-evaluating true).
"""
from __future__ import annotations
import sys
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from strategies.indicators import ema, atr  # noqa: E402

EMA_FAST, EMA_SLOW = 20, 50
FIXED_CONFIDENCE = "medium"
FIXED_RR = 1.0  # 1:1 reward:risk -- a single fixed tier, documented, not tuned


def trend_pullback_signal(h4_mid: pd.DataFrame) -> pd.Series:
    close, low, high = h4_mid["close"], h4_mid["low"], h4_mid["high"]
    ema_fast = ema(close, EMA_FAST)
    ema_slow = ema(close, EMA_SLOW)

    uptrend = ema_fast > ema_slow
    downtrend = ema_fast < ema_slow

    pulled_back_up = low.shift(1) <= ema_fast.shift(1)
    resumed_up = (close > ema_fast) & (close > close.shift(1))
    long_signal = uptrend & pulled_back_up & resumed_up

    pulled_back_down = high.shift(1) >= ema_fast.shift(1)
    resumed_down = (close < ema_fast) & (close < close.shift(1))
    short_signal = downtrend & pulled_back_down & resumed_down

    score = pd.Series(0.0, index=h4_mid.index)
    score[long_signal] = 1.0
    score[short_signal] = -1.0
    fresh = score != score.shift(1).fillna(0.0)
    return score.where(fresh, 0.0)


def trend_pullback_levels(h4_mid: pd.DataFrame) -> pd.DataFrame:
    """Returns direction/confidence/entry/stop/target for every bar (0/""
    where no signal fired). ATR-based stop/target, matching the SAME
    ATR-multiple structure the baseline/genuine-ORB candidates use (only
    the entry rule differs between candidates, so cost/risk mechanics stay
    comparable) -- sl_near=1.5xATR, tp_far=FIXED_RR(1.0)xATR, a single
    fixed tier since this strategy has no confidence-magnitude to tier by."""
    a = atr(h4_mid, 14)
    signal = trend_pullback_signal(h4_mid)
    direction = signal.map({1.0: "long", -1.0: "short", 0.0: "no_trade"})
    entry = h4_mid["close"]
    sl_near = 1.5 * a
    tp_far = FIXED_RR * a
    stop = pd.Series(index=h4_mid.index, dtype=float)
    target = pd.Series(index=h4_mid.index, dtype=float)
    long_mask, short_mask = signal == 1.0, signal == -1.0
    stop[long_mask] = entry[long_mask] - sl_near[long_mask]
    target[long_mask] = entry[long_mask] + tp_far[long_mask]
    stop[short_mask] = entry[short_mask] + sl_near[short_mask]
    target[short_mask] = entry[short_mask] - tp_far[short_mask]
    return pd.DataFrame({"direction": direction, "confidence": FIXED_CONFIDENCE, "entry": entry,
                          "stop": stop, "target": target, "atr": a}, index=h4_mid.index)
