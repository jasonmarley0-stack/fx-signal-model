"""Verifies the DST fix in sessions.py and the one-shot-per-day fix in
opening_range_breakout.py, since both were silent correctness bugs found
after a real backtest (composite/ORB underperforming — traced to the
opening range being measured an hour early in winter, and ORB re-firing
on every extended bar instead of once at the actual breakout).
"""
import sys
from pathlib import Path
from datetime import date, timezone
import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from sessions import session_open_datetime
from strategies.opening_range_breakout import orb_signal


def test_london_dst_handling():
    # Winter (GMT, no DST): London local 08:00 = 08:00 UTC
    winter_open = session_open_datetime("london", date(2026, 1, 15))
    assert winter_open.hour == 8, f"expected 8 UTC in winter, got {winter_open.hour}"

    # Summer (BST, UTC+1): London local 08:00 = 07:00 UTC
    summer_open = session_open_datetime("london", date(2026, 7, 15))
    assert summer_open.hour == 7, f"expected 7 UTC in summer (BST), got {summer_open.hour}"

    print("London DST handling: OK (winter=08:00 UTC, summer=07:00 UTC)")


def _make_breakout_day(date_str: str, or_start_utc: str, extended_bars: int = 5) -> pd.DataFrame:
    """Builds one day of 30-min bars: a flat opening range, then a clean
    breakout that stays extended for several bars (the scenario that used
    to cause repeated re-firing)."""
    idx = pd.date_range(f"{date_str} 00:00", periods=48, freq="30min", tz="UTC")
    base = 1.3000
    open_ = np.full(48, base)
    high = np.full(48, base + 0.0005)
    low = np.full(48, base - 0.0005)
    close = np.full(48, base)

    or_start_idx = idx.get_loc(pd.Timestamp(f"{date_str} {or_start_utc}", tz="UTC"))
    # after the opening range bar, price breaks out and stays extended
    for i in range(or_start_idx + 1, or_start_idx + 1 + extended_bars):
        close[i] = base + 0.0030 + 0.0002 * (i - or_start_idx)  # clearly beyond range, extending further
        high[i] = close[i] + 0.0003
        low[i] = close[i] - 0.0003
        open_[i] = close[i] - 0.0001

    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                          "volume": np.full(48, 500)}, index=idx)


def test_orb_fires_once_not_repeatedly():
    df = _make_breakout_day("2026-01-15", "08:00", extended_bars=5)
    score = orb_signal(df, session_open="london", min_atr_multiple=0.01, confirm_bars=1)

    fires = score[score != 0]
    assert len(fires) == 1, f"expected exactly 1 signal for the whole extended move, got {len(fires)}: {fires}"
    assert fires.iloc[0] == 1.0
    print(f"ORB one-shot firing: OK (1 signal fired, at {fires.index[0]}, for a {5}-bar extended move)")


if __name__ == "__main__":
    test_london_dst_handling()
    test_orb_fires_once_not_repeatedly()
    print("All session/ORB correctness tests passed.")
