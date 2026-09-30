"""Dedicated deadline-boundary tests, added after finding that the
completed-candle stop/target scan was including a candle whose OWN OPEN
was at-or-before the holding deadline but whose FULL SPAN extended past
it -- letting that candle's high/low (partly reflecting price action
after the deadline) resolve a decision that should have been bounded by
the deadline. Fixed in replay_scorer.py: only fully-completed candles
(open + interval <= deadline) may contribute a stop/target/ambiguity hit
via high/low; the single candle whose open lands EXACTLY on the deadline
(this system's entries are always confirmed on a grid-aligned M30 open,
and max_holding_time_hours is always a whole multiple of the M30
interval, so such a candle exists whenever the data isn't gapped there)
may only contribute via its own open (a gap-through check, or the
time-exit price itself) -- never its high/low. Without an exact-deadline
quote, the outcome is incomplete_coverage, not a fabricated fill from an
earlier, stale open.

Synthetic fixtures throughout -- correctness checks on the harness, not
strategy evidence (see RESULTS.md for that).
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from replay_scorer import score_replay_version  # noqa: E402

T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)  # a real Monday, grid-aligned to the M30 boundary
HOLD_HOURS = 30.0  # this system's real, only-ever-used max_holding_time_hours (a whole multiple of 30 minutes)
DEADLINE = T0 + timedelta(hours=HOLD_HOURS)  # always exactly grid-aligned given a grid-aligned entry


def _bars(rows: dict) -> pd.DataFrame:
    idx = sorted(rows)
    return pd.DataFrame([rows[t] for t in idx], index=pd.DatetimeIndex(idx, tz="UTC"))


def _flat(times, price) -> dict:
    return {t: {"open": price, "high": price + 0.00002, "low": price - 0.00002, "close": price} for t in times}


def _version(**overrides) -> dict:
    base = dict(pair="EURUSD", direction="long", entry=1.10000, entry_condition_lo=1.09950,
                entry_condition_hi=1.10050, stop=1.09000, target=1.20000,  # deliberately far away -- nothing hit before the deadline
                published_at=T0, max_holding_time_hours=HOLD_HOURS)
    base.update(overrides)
    return base


def test_grid_aligned_deadline_exact_quote_time_exit_both_directions():
    """No stop/target hit anywhere; the deadline lands exactly on a real
    quote (grid-aligned entry + a whole-multiple-of-interval hold) — that
    exact quote is used as the time-exit price, for both a long (checked
    on the bid/exit series) and a short (mirrored)."""
    for direction, stop, target in (("long", 1.09000, 1.20000), ("short", 1.20000, 1.09000)):
        version = _version(direction=direction, stop=stop, target=target)
        times = [T0, T0 + timedelta(minutes=30), DEADLINE]
        exit_series = _bars(_flat(times, 1.10000))
        exit_series.loc[DEADLINE, ["open", "high", "low", "close"]] = [1.10500, 1.10520, 1.10480, 1.10510]
        entry_series = _bars(_flat(times, 1.10010))
        m30_bid = exit_series if direction == "long" else entry_series
        m30_ask = entry_series if direction == "long" else exit_series
        result = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", m30_bid, m30_ask, now=DEADLINE + timedelta(hours=1))
        assert result["state"] == "time_exited", (direction, result)
        assert result["exit_price"] == 1.10500, (direction, result["exit_price"])
        assert result["exit_time_utc"] == DEADLINE.isoformat()
    print("deadline boundary: exact grid-aligned quote at the deadline used for time exit, both directions: OK")


def test_deadline_exact_quote_gap_through_stop_and_target():
    """The exact-deadline candle's own OPEN already shows a gap through the
    stop (or target) -- a real observation at a time == the deadline. Must
    resolve as stopped/targeted at that open, not a plain time exit."""
    # gap through stop
    version = _version(direction="long", stop=1.09000, target=1.20000)
    times = [T0, DEADLINE]
    exit_series = _bars(_flat(times, 1.10000))
    exit_series.loc[DEADLINE, ["open", "high", "low", "close"]] = [1.08900, 1.08950, 1.08850, 1.08900]  # gapped below stop at its own open
    entry_series = _bars(_flat(times, 1.10010))
    result = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", exit_series, entry_series, now=DEADLINE + timedelta(hours=1))
    assert result["state"] == "stopped", result
    assert result["exit_price"] == 1.08900, "must price at the gapped-through open, not the unquoted exact stop level"

    # gap through target
    version = _version(direction="long", stop=1.05000, target=1.10500)
    exit_series2 = _bars(_flat(times, 1.10000))
    exit_series2.loc[DEADLINE, ["open", "high", "low", "close"]] = [1.10600, 1.10650, 1.10550, 1.10600]  # gapped above target at open
    result2 = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", exit_series2, entry_series, now=DEADLINE + timedelta(hours=1))
    assert result2["state"] == "targeted", result2
    assert result2["exit_price"] == 1.10600
    print("deadline boundary: a gap-through at the exact-deadline quote resolves stopped/targeted at that open: OK")


def test_deadline_exact_quote_both_levels_gapped_is_ambiguous():
    """A single gapped open can satisfy both hit_stop_at_open (open <=
    stop, for a long) and hit_target_at_open (open >= target) at once when
    target <= stop -- constructed deliberately to trigger both checks from
    one observed quote, which cannot be ordered and must be ambiguous.
    The exit (bid) series deliberately has NO bar before the deadline, so
    there is nothing earlier for the completed-candle scan to (correctly)
    react to -- isolating the exact-deadline-quote ambiguity check."""
    entry_series = _bars(_flat([T0], 1.10010))  # only needs to confirm entry
    version = _version(direction="long", stop=1.09600, target=1.09400)
    exit_series = _bars({DEADLINE: {"open": 1.09500, "high": 1.09550, "low": 1.09450, "close": 1.09500}})  # open<=stop(1.096) AND open>=target(1.094)
    result = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", exit_series, entry_series, now=DEADLINE + timedelta(hours=1))
    assert result["state"] == "ambiguous_intrabar_exit", result
    assert result["r_multiple"] is None
    print("deadline boundary: both levels breached at the single exact-deadline quote is ambiguous, not guessed: OK")


def test_deadline_gap_no_exact_quote_is_incomplete_coverage():
    """No candle opens exactly at the deadline (a genuine data gap, e.g. a
    weekend) -- must report incomplete_coverage, never fabricate a fill
    from an earlier, stale quote."""
    version = _version(direction="long", stop=1.05000, target=1.20000)
    times = [T0, T0 + timedelta(minutes=30), DEADLINE - timedelta(hours=3)]  # nothing at or near DEADLINE itself
    exit_series = _bars(_flat(times, 1.10000))
    entry_series = _bars(_flat(times, 1.10010))
    result = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", exit_series, entry_series, now=DEADLINE + timedelta(hours=1))
    assert result["state"] == "incomplete_coverage", result
    assert result["exit_price"] is None
    print("deadline boundary: no quote exactly at the deadline (data gap) is incomplete_coverage, not a fabricated fill: OK")


def test_straddling_near_deadline_candle_not_used_for_highlow_but_completed_earlier_hits_still_resolve():
    """The actual bug this file exists to catch: a candle whose open is
    BEFORE the deadline but whose completion (open+interval) extends past
    it must NOT have its high/low used to resolve a stop/target — even
    though it is the last available candle before the deadline. Paired
    with a check that a stop/target hit on an ordinary, FULLY COMPLETED
    candle well before the deadline still resolves normally (the fix must
    not break normal mid-holding-period resolution)."""
    # Part A: an early, fully-completed candle hits target normally -- resolves via high/low as before.
    version = _version(direction="long", stop=1.05000, target=1.10600, max_holding_time_hours=HOLD_HOURS)
    early_hit_time = T0 + timedelta(hours=2)
    times_a = [T0, early_hit_time]
    exit_series_a = _bars(_flat(times_a, 1.10000))
    exit_series_a.loc[early_hit_time, ["open", "high", "low", "close"]] = [1.10050, 1.10650, 1.10040, 1.10600]  # clears target via high, well within the hold, far from the deadline
    entry_series_a = _bars(_flat(times_a, 1.10010))
    result_a = score_replay_version(version, T0 + timedelta(hours=1), "entry_expiry", exit_series_a, entry_series_a, now=DEADLINE + timedelta(hours=1))
    assert result_a["state"] == "targeted", result_a
    assert result_a["exit_time_utc"] == early_hit_time.isoformat()

    # Part B: only a candle STRADDLING a shorter, non-grid-aligned deadline could resolve it via high/low --
    # must NOT be used; with nothing else available, the correct outcome is incomplete_coverage.
    short_hold_version = _version(direction="long", stop=1.05000, target=1.10600, max_holding_time_hours=0.25)  # 15-minute hold again
    straddle_open = T0
    times_b = [straddle_open]
    exit_series_b = _bars(_flat(times_b, 1.10000))
    # this candle's full high/low would clear target, but its OWN OPEN does not, and its span (T0..T0+30min)
    # straddles the 15-minute deadline rather than opening exactly on it
    exit_series_b.loc[straddle_open, ["open", "high", "low", "close"]] = [1.10010, 1.10650, 1.09990, 1.10600]
    entry_series_b = _bars(_flat(times_b, 1.10010))
    short_deadline = straddle_open + timedelta(minutes=15)
    result_b = score_replay_version(short_hold_version, T0 + timedelta(hours=1), "entry_expiry", exit_series_b, entry_series_b, now=short_deadline + timedelta(hours=1))
    assert result_b["state"] == "incomplete_coverage", (
        f"a straddling (not-yet-completed-by-the-deadline) candle's high/low must not resolve the trade, got {result_b['state']}")
    print("deadline boundary: a straddling candle's high/low is never used at the boundary; earlier completed hits still resolve normally: OK")


if __name__ == "__main__":
    test_grid_aligned_deadline_exact_quote_time_exit_both_directions()
    test_deadline_exact_quote_gap_through_stop_and_target()
    test_deadline_exact_quote_both_levels_gapped_is_ambiguous()
    test_deadline_gap_no_exact_quote_is_incomplete_coverage()
    test_straddling_near_deadline_candle_not_used_for_highlow_but_completed_earlier_hits_still_resolve()
    print("All deadline-boundary tests passed.")
