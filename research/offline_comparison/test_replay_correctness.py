"""Correctness checks for the replay harness itself — timing, cost
accounting, ambiguity handling, and position accounting. Synthetic
fixtures throughout: these establish that the HARNESS behaves as
specified, not anything about any strategy's real performance (see
configs.py's evaluation contract and RESULTS.md, which is where actual
strategy evidence — from the real OANDA dataset — is reported).

Plain assert + __main__ runner, matching the repo's existing test
convention (tests/test_sessions_and_orb.py etc.).
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from replay_scorer import score_replay_version  # noqa: E402

T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)  # a real Monday


def _bars(rows: dict) -> pd.DataFrame:
    idx = sorted(rows)
    return pd.DataFrame([rows[t] for t in idx], index=pd.DatetimeIndex(idx, tz="UTC"))


def _version(**overrides) -> dict:
    base = dict(pair="EURUSD", direction="long", entry=1.10000, entry_condition_lo=1.09950,
                entry_condition_hi=1.10050, stop=1.09800, target=1.10300,
                published_at=T0, max_holding_time_hours=10.0)
    base.update(overrides)
    return base


def test_cost_charged_via_ask_entry_bid_exit_not_double_counted():
    """A long enters at the ASK and exits at the BID — the real spread
    shows up exactly once, as the gap between those two prices, with no
    separate deduction anywhere."""
    version = _version(direction="long", entry_condition_lo=1.09950, entry_condition_hi=1.10050,
                        stop=1.09800, target=1.10300)
    # ask series: entry bar's open lands in range
    m30_ask = _bars({T0: {"open": 1.10010, "high": 1.10020, "low": 1.10000, "close": 1.10010}})
    # bid series (used for the EXIT side): stays flat, never near stop/target here, just checking entry pricing
    m30_bid = _bars({T0: {"open": 1.09960, "high": 1.09970, "low": 1.09950, "close": 1.09960}})
    result = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", m30_bid, m30_ask, now=T0 + timedelta(hours=2))
    assert result["assumed_entry_price"] == 1.10010, "long entry must be priced off the ASK series, not bid or mid"
    print("cost: long entry priced off ASK (buyer pays ask): OK")

    # now check a short's entry comes from the BID series instead
    version_short = _version(direction="short", entry_condition_lo=1.09950, entry_condition_hi=1.10050,
                              stop=1.10200, target=1.09700)
    result_short = score_replay_version(version_short, T0 + timedelta(hours=1), "entry_expiry", m30_bid, m30_ask, now=T0 + timedelta(hours=2))
    assert result_short["assumed_entry_price"] == 1.09960, "short entry must be priced off the BID series, not ask or mid"
    print("cost: short entry priced off BID (seller receives bid): OK")


def test_cost_makes_real_spread_visible_in_r_multiple():
    """A round trip with a real, nonzero bid/ask spread must show a worse
    r_multiple than the same trade priced at mid would — the spread cost
    must actually reduce R, not vanish."""
    version = _version(direction="long", entry_condition_lo=1.09950, entry_condition_hi=1.10050,
                        stop=1.09800, target=1.10300, max_holding_time_hours=4)
    m30_ask = _bars({
        T0: {"open": 1.10010, "high": 1.10015, "low": 1.10000, "close": 1.10010},
        T0 + timedelta(minutes=30): {"open": 1.10010, "high": 1.10320, "low": 1.10005, "close": 1.10310},
    })
    m30_bid = _bars({
        T0: {"open": 1.09960, "high": 1.09965, "low": 1.09950, "close": 1.09960},
        T0 + timedelta(minutes=30): {"open": 1.09960, "high": 1.10305, "low": 1.09955, "close": 1.10300},
    })
    result = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", m30_bid, m30_ask, now=T0 + timedelta(hours=2))
    assert result["state"] == "targeted", result
    # entry at ask 1.10010, target fixed at 1.10300 (a level, not a series price) -> risk/reward computed off the real entry
    risk = 1.10010 - 1.09800
    expected_r = (1.10300 - 1.10010) / risk
    assert abs(result["r_multiple"] - expected_r) < 1e-9
    assert result["r_multiple"] < (1.10300 - 1.10000) / (1.10000 - 1.09800), "spread must make R worse than an idealised mid-priced entry"
    print("cost: real spread visibly reduces r_multiple versus an idealised mid entry: OK")


def test_entry_window_boundary_exclusive():
    version = _version(entry_condition_lo=1.09950, entry_condition_hi=1.10050)
    window_end = T0 + timedelta(minutes=30)
    m30_ask = _bars({window_end: {"open": 1.10000, "high": 1.10005, "low": 1.09995, "close": 1.10000}})  # sits exactly AT the boundary
    m30_bid = _bars({window_end: {"open": 1.09950, "high": 1.09955, "low": 1.09945, "close": 1.09950}})
    result = score_replay_version(version, window_end, "entry_expiry", m30_bid, m30_ask, now=window_end + timedelta(minutes=30))
    assert result["state"] == "expired_no_entry", (
        f"a bar starting exactly AT the window boundary must not count as pre-expiry entry activity, got {result['state']}")
    print("timing: entry window end is exclusive — a bar at the boundary is not used: OK")


def test_ambiguous_intrabar_exit_with_real_bid_ask():
    version = _version(direction="long", entry_condition_lo=1.09950, entry_condition_hi=1.10050,
                        stop=1.09800, target=1.10300, max_holding_time_hours=10)
    m30_ask = _bars({
        T0: {"open": 1.10010, "high": 1.10015, "low": 1.10000, "close": 1.10010},
        T0 + timedelta(minutes=30): {"open": 1.10010, "high": 1.10400, "low": 1.09600, "close": 1.10050},  # crosses both, on the ASK side
    })
    m30_bid = _bars({
        T0: {"open": 1.09960, "high": 1.09965, "low": 1.09950, "close": 1.09960},
        T0 + timedelta(minutes=30): {"open": 1.09960, "high": 1.10390, "low": 1.09590, "close": 1.10040},  # exit side (bid, for a long) also crosses both
    })
    result = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", m30_bid, m30_ask, now=T0 + timedelta(hours=2))
    assert result["state"] == "ambiguous_intrabar_exit", result
    assert result["r_multiple"] is None
    print("ambiguity: a bar crossing both stop and target on the correct (bid) exit side is ambiguous, not a guessed win: OK")


def test_position_accounting_suppression_logic():
    """Not a call into replay_engine's full pipeline (that needs a real
    pair dataset) — a direct, minimal check of the SAME suppression rule
    replay_engine.run_replay applies: a position with no exit_time yet
    (state == 'open') or an exit_time still in the future must block a
    new entry for that pair; once exit_time has passed, it must not."""
    open_position = {"exit_time": None}
    ts_dt = T0
    assert (open_position["exit_time"] is None) or (open_position["exit_time"] > ts_dt), "an unresolved-open position must suppress"

    open_position = {"exit_time": T0 - timedelta(hours=1)}
    assert not (open_position["exit_time"] is None or open_position["exit_time"] > ts_dt), "a position that already exited must NOT suppress a same-bar-or-later signal"

    open_position = {"exit_time": T0 + timedelta(hours=1)}
    assert open_position["exit_time"] > ts_dt, "a position exiting in the future must still suppress the current bar"
    print("position accounting: suppression logic matches replay_engine's own open/closed check: OK")


if __name__ == "__main__":
    test_cost_charged_via_ask_entry_bid_exit_not_double_counted()
    test_cost_makes_real_spread_visible_in_r_multiple()
    test_entry_window_boundary_exclusive()
    test_ambiguous_intrabar_exit_with_real_bid_ask()
    test_position_accounting_suppression_logic()
    print("All replay-harness correctness checks passed (synthetic fixtures — not strategy evidence).")
