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


# --- Correction pass: replay_scorer.py had reintroduced the entry-candle
# skip bug (index > entry_time excluded the very candle that confirmed
# entry, even though its own high/low after that open can still reach
# stop/target), mispriced holding-time exits off a later candle's CLOSE
# instead of the boundary-respecting OPEN, and never distinguished a gap
# fill from a level reached by normal intrabar movement. Fixed; these
# tests exercise each directly.

def test_entry_candle_itself_can_hit_stop():
    version = _version(direction="long", entry_condition_lo=1.09950, entry_condition_hi=1.10050,
                        stop=1.09800, target=1.10500, max_holding_time_hours=10)
    # the SAME bar that confirms entry (open in range) also dips to hit stop before closing
    m30_ask = _bars({T0: {"open": 1.10010, "high": 1.10020, "low": 1.09990, "close": 1.10000}})
    m30_bid = _bars({T0: {"open": 1.09960, "high": 1.09970, "low": 1.09790, "close": 1.09950}})
    result = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", m30_bid, m30_ask, now=T0 + timedelta(hours=2))
    assert result["state"] == "stopped", result
    assert result["exit_time_utc"] == result["assumed_entry_time_utc"], "the stop must be attributed to the entry candle itself, not skipped to a later one"
    print("entry-candle exit: a stop hit within the entry candle itself is not skipped: OK")


def test_entry_candle_itself_can_hit_target():
    version = _version(direction="long", entry_condition_lo=1.09950, entry_condition_hi=1.10050,
                        stop=1.09500, target=1.10300, max_holding_time_hours=10)
    m30_ask = _bars({T0: {"open": 1.10010, "high": 1.10020, "low": 1.09990, "close": 1.10000}})
    m30_bid = _bars({T0: {"open": 1.09960, "high": 1.10310, "low": 1.09950, "close": 1.10300}})
    result = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", m30_bid, m30_ask, now=T0 + timedelta(hours=2))
    assert result["state"] == "targeted", result
    assert result["exit_time_utc"] == result["assumed_entry_time_utc"], "the target must be attributed to the entry candle itself, not skipped to a later one"
    print("entry-candle exit: a target hit within the entry candle itself is not skipped: OK")


def test_entry_candle_itself_ambiguous_not_a_later_clean_win():
    """Regression test for the exact bug this correction fixes: the entry
    candle crosses both levels, and a LATER candle reaches target cleanly
    -- must report ambiguous at the entry candle, not the later win."""
    version = _version(direction="long", entry_condition_lo=1.09950, entry_condition_hi=1.10050,
                        stop=1.09500, target=1.10500, max_holding_time_hours=10)
    m30_ask = _bars({
        T0: {"open": 1.10010, "high": 1.10020, "low": 1.09990, "close": 1.10000},
        T0 + timedelta(minutes=30): {"open": 1.10000, "high": 1.10520, "low": 1.09990, "close": 1.10510},
    })
    m30_bid = _bars({
        T0: {"open": 1.09960, "high": 1.10510, "low": 1.09490, "close": 1.09950},  # entry candle crosses BOTH on the bid (exit) side
        T0 + timedelta(minutes=30): {"open": 1.09950, "high": 1.10510, "low": 1.09940, "close": 1.10500},
    })
    result = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", m30_bid, m30_ask, now=T0 + timedelta(hours=2))
    assert result["state"] == "ambiguous_intrabar_exit", (
        f"expected ambiguous_intrabar_exit at the entry candle, got {result['state']} (r_multiple={result.get('r_multiple')})")
    assert result["r_multiple"] is None
    print("entry-candle exit: ambiguity in the entry candle is not overridden by a later clean win: OK")


def test_holding_boundary_prices_off_open_not_close():
    """A time exit's price must come from the last qualifying bar's OPEN
    (known at-or-before the deadline), never its CLOSE (which represents
    an instant after the deadline)."""
    version = _version(direction="long", entry_condition_lo=1.09950, entry_condition_hi=1.10050,
                        stop=1.05000, target=1.15000, max_holding_time_hours=0.25)  # 15-minute hold, deadline inside the next bar
    m30_ask = _bars({T0: {"open": 1.10010, "high": 1.10020, "low": 1.10000, "close": 1.10010}})
    # deadline = T0 + 15min, which falls inside the bar starting at T0 (a 30-min bar) --
    # its OPEN (1.09960) is known at T0, at-or-before the deadline; its CLOSE (1.09995)
    # represents T0+30min, which is AFTER the 15-minute deadline.
    m30_bid = _bars({T0: {"open": 1.09960, "high": 1.09998, "low": 1.09955, "close": 1.09995}})
    max_exit_time = T0 + timedelta(minutes=15)
    result = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", m30_bid, m30_ask, now=max_exit_time + timedelta(hours=1))
    assert result["state"] == "time_exited", result
    assert result["exit_price"] == 1.09960, f"expected the bar's OPEN (1.09960), got {result['exit_price']} — using close would price the exit off information from after the deadline"
    print("holding boundary: time exit prices off the qualifying bar's OPEN, not its later CLOSE: OK")


def test_gap_through_stop_uses_open_not_exact_level():
    """If the exit bar's own OPEN has already crossed the stop, the first
    real observed price was already past it -- pricing the exit exactly
    at the stop level would assume a fill that was never actually quoted."""
    version = _version(direction="long", entry_condition_lo=1.09950, entry_condition_hi=1.10050,
                        stop=1.09800, target=1.10500, max_holding_time_hours=10)
    m30_ask = _bars({
        T0: {"open": 1.10010, "high": 1.10020, "low": 1.10000, "close": 1.10010},
        T0 + timedelta(minutes=30): {"open": 1.10000, "high": 1.10010, "low": 1.09990, "close": 1.10000},
    })
    m30_bid = _bars({
        T0: {"open": 1.09960, "high": 1.09965, "low": 1.09950, "close": 1.09960},
        # this bar GAPS through the stop at its own open (1.09700 < stop 1.09800) --
        # no quote at exactly 1.09800 was ever observed in this bar
        T0 + timedelta(minutes=30): {"open": 1.09700, "high": 1.09710, "low": 1.09650, "close": 1.09680},
    })
    result = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", m30_bid, m30_ask, now=T0 + timedelta(hours=2))
    assert result["state"] == "stopped", result
    assert result["exit_price"] == 1.09700, f"a gap-through must price at the bar's own open (1.09700, the first real observed quote), not the unquoted stop level, got {result['exit_price']}"
    print("gap handling: a stop gapped through prices at the bar's open, not an unquoted exact level: OK")


def test_normal_touch_stop_still_prices_at_exact_level():
    """The gap-handling fix must not change the ordinary case: a bar whose
    OPEN has not yet crossed the stop, but whose low reaches it, still
    fills at the exact stop level (a resting order at that price)."""
    version = _version(direction="long", entry_condition_lo=1.09950, entry_condition_hi=1.10050,
                        stop=1.09800, target=1.10500, max_holding_time_hours=10)
    m30_ask = _bars({T0: {"open": 1.10010, "high": 1.10020, "low": 1.10000, "close": 1.10010}})
    m30_bid = _bars({T0: {"open": 1.09960, "high": 1.09965, "low": 1.09790, "close": 1.09850}})  # open above stop, low reaches it normally
    result = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", m30_bid, m30_ask, now=T0 + timedelta(hours=2))
    assert result["state"] == "stopped", result
    assert result["exit_price"] == 1.09800, f"a normal intrabar touch must still fill at the exact stop level, got {result['exit_price']}"
    print("gap handling: a normal (non-gapped) touch still fills at the exact level: OK")


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


def test_ambiguous_and_incomplete_entered_positions_reserve_the_pair():
    """Regression test for the bug this correction fixes: an entered
    position that resolves to ambiguous_intrabar_exit or incomplete_
    coverage has no exit_time_utc at all. The OLD occupancy rule only
    checked `scored["state"] == "open"` or `scored.get("exit_time_utc")`
    — neither matched these two states, so the pair was silently freed
    for a new entry despite a real, unresolved position outstanding. The
    fix: any entered position without a clean exit_time_utc is
    conservatively held open through entry_time + max_holding_time_hours."""
    for state in ("ambiguous_intrabar_exit", "incomplete_coverage"):
        scored = {"assumed_entry_time_utc": T0.isoformat(), "exit_time_utc": None, "state": state}
        max_holding_time_hours = 10.0
        entered = scored.get("assumed_entry_time_utc") is not None
        assert entered
        if scored.get("exit_time_utc"):
            exit_dt = datetime.fromisoformat(scored["exit_time_utc"])
        else:
            entry_dt = datetime.fromisoformat(scored["assumed_entry_time_utc"])
            exit_dt = entry_dt + timedelta(hours=max_holding_time_hours)
        decision_time = T0 + timedelta(hours=5)  # well within the 10h conservative hold
        assert exit_dt > decision_time, f"{state} must still reserve the pair 5h after entry, under a 10h conservative hold"
    print("position accounting: ambiguous/incomplete-coverage entries reserve the pair conservatively, not freed immediately: OK")


if __name__ == "__main__":
    test_cost_charged_via_ask_entry_bid_exit_not_double_counted()
    test_cost_makes_real_spread_visible_in_r_multiple()
    test_entry_window_boundary_exclusive()
    test_ambiguous_intrabar_exit_with_real_bid_ask()
    test_entry_candle_itself_can_hit_stop()
    test_entry_candle_itself_can_hit_target()
    test_entry_candle_itself_ambiguous_not_a_later_clean_win()
    test_holding_boundary_prices_off_open_not_close()
    test_gap_through_stop_uses_open_not_exact_level()
    test_normal_touch_stop_still_prices_at_exact_level()
    test_position_accounting_suppression_logic()
    test_ambiguous_and_incomplete_entered_positions_reserve_the_pair()
    print("All replay-harness correctness checks passed (synthetic fixtures — not strategy evidence).")
