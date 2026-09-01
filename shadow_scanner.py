"""Shadow/paper-mode scanner for the grid-search winner (see NEXT_STEPS.md,
"H1 reweight" backtest, 2026-09-01): H1 bars, ORB/trend/pattern reweighted
0.35/0.40/0.25 (was 0.5/0.3/0.2), entry threshold 0.40, tech-only — no
PESTLE (alpha=1.0). That configuration was the single most statistically
credible result out of ~109 backtested scenarios (2,347 resolved trades,
win_rate=51.0%, avg_r=+0.02, total_r=+47R), but a backtest — even a careful
one — isn't proof against a live, forward-only feed. This logs what that
configuration WOULD have signalled, on real live prices, without those
signals counting as real trades or touching the live product
(streaming_scanner.py, alerts.json, performance.json are untouched).

Deliberately a periodic REST poll, not a persistent tick stream: H1 bars
only close once an hour, and fetch_oanda_candles() already excludes the
in-progress candle (see data/oanda.py) — so a poll every
POLL_INTERVAL_MINUTES minutes sees a fresh closed bar promptly without any
of the live-partial-bar noise that turned out to be streaming_scanner.py's
dominant problem (see NEXT_STEPS.md, "closed-bar vs live-bar" investigation).

Usage:
    python3 shadow_scanner.py

Meant to run under systemd on a recurring timer (see
setup/shadow-scanner.service + .timer), not interactively.
"""
from __future__ import annotations
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from data.oanda import fetch_oanda_candles, fetch_current_price  # noqa: E402
from strategies.indicators import atr  # noqa: E402
from strategies.opening_range_breakout import orb_signal  # noqa: E402
from strategies.trend_following import trend_signal  # noqa: E402
from strategies.candlestick_patterns import pattern_signal  # noqa: E402
from combiner import combine_signal  # noqa: E402

PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
    "EURGBP", "EURJPY", "EURCHF", "EURAUD", "EURCAD", "EURNZD",
    "GBPJPY", "GBPCHF", "GBPAUD", "GBPCAD", "GBPNZD",
    "AUDJPY", "AUDCHF", "AUDCAD", "AUDNZD",
    "NZDJPY", "NZDCHF", "NZDCAD",
    "CADJPY", "CADCHF", "CHFJPY",
]
GRANULARITY = "H1"
HISTORY_BARS = 300  # ~12.5 days — plenty of warmup for EMA50/ATR14
REWEIGHT_W = {"orb": 0.35, "trend": 0.40, "pattern": 0.25}
ENTRY_THRESHOLD = 0.40  # backtested winner; higher than combiner.CONFIDENCE_MEDIUM (0.35)
ALPHA = 1.0  # tech-only — PESTLE couldn't be tested at this scale (see NEXT_STEPS.md), so it's left out here

STATE_PATH = Path(__file__).parent / ".shadow_state.json"
SHADOW_LOG_DIR = Path(__file__).parent / "shadow_signals_log"


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def reweighted_tech_score(df) -> tuple[float, float]:
    """Returns (tech_score, atr_value) at the most recent closed bar, using
    the backtested reweight variant — same components as
    strategies.composite.technical_score, different weights, no RSI/volume
    dampening (the backtest's "reweight" alone beat "rsi", "volume", and
    "all_four" at scale — see NEXT_STEPS.md)."""
    a = atr(df)
    tmp = df.copy()
    tmp["date"] = tmp.index.date
    daily = tmp.groupby("date").agg(day_high=("high", "max"), day_low=("low", "min"))
    prior_day_high = tmp["date"].map(daily["day_high"].shift(1))
    prior_day_low = tmp["date"].map(daily["day_low"].shift(1))

    orb = orb_signal(df)
    trend = trend_signal(df)
    pattern = pattern_signal(df, atr=a, prior_day_high=prior_day_high, prior_day_low=prior_day_low)

    tech = REWEIGHT_W["orb"] * orb + REWEIGHT_W["trend"] * trend + REWEIGHT_W["pattern"] * pattern
    disagree = (orb != 0) & (trend != 0) & (orb * trend < 0) & (trend.abs() == 1)
    tech[disagree] = tech[disagree] * 0.5
    tech = tech.clip(-1.0, 1.0)
    return float(tech.iloc[-1]), float(a.iloc[-1])


def log_shadow_signal(pair: str, sig) -> None:
    if sig.direction == "no_trade":
        return
    SHADOW_LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = SHADOW_LOG_DIR / f"{today}.jsonl"
    entry = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "pair": pair,
        "direction": sig.direction,
        "confidence": sig.confidence,
        "combined_score": sig.combined_score,
        "tech_score": sig.tech_score,
        "pestle_score": sig.pestle_score,
        "entry": sig.entry,
        "stop_loss_range": list(sig.stop_loss_range),
        "take_profit_range": list(sig.take_profit_range),
        "reason": sig.reason,
        "window": None,
    }
    with log_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[shadow fire] {pair}: {sig.direction.upper()} ({sig.confidence}) @ {sig.entry:.5f} — {sig.reason}")


def main() -> None:
    state = load_state()
    for pair in PAIRS:
        try:
            df = fetch_oanda_candles(pair, granularity=GRANULARITY, count=HISTORY_BARS)
            if len(df) < 60:
                print(f"  {pair}: not enough history yet ({len(df)} bars)")
                continue
            tech_score, atr_value = reweighted_tech_score(df)
            if atr_value <= 0:
                continue
            entry_price = fetch_current_price(pair)
            magnitude = abs(tech_score)
            direction = "long" if tech_score > 0 else ("short" if tech_score < 0 else None)
            new_direction = direction if magnitude >= ENTRY_THRESHOLD else None

            prev = state.get(pair)
            state[pair] = new_direction
            if prev is None:
                continue  # first run — establish baseline, don't fire on pre-existing state
            if new_direction == prev or new_direction is None:
                continue

            sig = combine_signal(pair=pair, entry=entry_price, atr_value=atr_value,
                                  tech_score=tech_score, pestle_score=0.0, alpha=ALPHA)
            if sig.direction != "no_trade":
                log_shadow_signal(pair, sig)
        except Exception as ex:
            print(f"  {pair}: error — {ex}")
        time.sleep(0.2)  # light pacing across 28 sequential OANDA calls, not a rate-limit workaround

    save_state(state)


if __name__ == "__main__":
    main()
