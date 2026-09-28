"""Scorer edge-case tests added per Codex's review of the first version of
the alert lifecycle change: expiry-boundary candles, intrabar ambiguity, an
entry price outside the condition range, missing candles at a time exit,
and per-pair-isolated fetch errors / coverage windows that grow with
history. Plain assert + __main__ runner, matching the existing convention.
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from alert_lifecycle import AlertLifecycleStore, classify_and_record  # noqa: E402
from alert_scorer import score_version, score_all  # noqa: E402

T0 = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


def _store():
    import tempfile
    return AlertLifecycleStore(Path(tempfile.mkdtemp()) / "alert_lifecycle_log.jsonl")


def _issue(store, **overrides):
    base = dict(
        scanner_version="TEST_v1", pair="EURUSD", direction="long", confidence="medium",
        combined_score=0.5, entry_price=1.1000, atr_value=0.0050, stop=1.0950, target=1.1075,
        technical_inputs={}, pestle_inputs=None, pestle_used=False, reason="test",
        calculated_at=T0, published_at=T0,
    )
    base.update(overrides)
    events = classify_and_record(store, **base)
    return events[-1]


def _bars(rows: dict[datetime, dict]) -> pd.DataFrame:
    idx = sorted(rows)
    return pd.DataFrame([rows[t] for t in idx], index=pd.DatetimeIndex(idx, tz="UTC"))


def test_expiry_boundary_bar_excluded():
    """A bar starting exactly AT (or after) entry_expiry_utc must not be
    used to confirm an entry — the entry window is [published_at, expiry),
    expiry itself is exclusive."""
    store = _store()
    issued = _issue(store, entry_price=1.1000, atr_value=0.0050)  # entry_condition ~ [1.09950, 1.10050]
    expiry = datetime.fromisoformat(issued["entry_expiry_utc"])

    # no bar before expiry ever enters the range; the ONLY qualifying bar
    # starts exactly at expiry — must be rejected, not counted as an entry
    rows = {}
    t = T0
    while t < expiry:
        rows[t] = {"open": 1.2000, "high": 1.2005, "low": 1.1995, "close": 1.2000}
        t += timedelta(minutes=30)
    rows[expiry] = {"open": 1.1000, "high": 1.1005, "low": 1.0995, "close": 1.1000}  # would confirm if wrongly included
    candles = _bars(rows)

    result = score_version(issued, [issued], candles, now=expiry + timedelta(minutes=30))
    assert result["state"] == "expired_no_entry", result
    assert result["assumed_entry_time_utc"] is None
    print("expiry-boundary bar correctly excluded: OK")


def test_intrabar_stop_and_target_ambiguous():
    """A single bar whose range crosses BOTH stop and target cannot have
    its order determined from M30 OHLC — must be 'ambiguous_intrabar_exit',
    not silently resolved as stop-first (or target-first)."""
    store = _store()
    issued = _issue(store, entry_price=1.1000, stop=1.0950, target=1.1075, atr_value=0.0050)
    rows = {
        T0: {"open": 1.1000, "high": 1.1002, "low": 1.0998, "close": 1.1000},           # confirms entry
        T0 + timedelta(minutes=30): {"open": 1.1000, "high": 1.1090, "low": 1.0900, "close": 1.1020},  # crosses BOTH levels
    }
    candles = _bars(rows)
    result = score_version(issued, [issued], candles, now=T0 + timedelta(hours=1))
    assert result["state"] == "ambiguous_intrabar_exit", result
    assert result["exit_price"] is None
    print("intrabar stop/target ambiguity: OK")


def test_entry_price_outside_condition_not_fabricated():
    """A bar whose high/low merely touches the entry range, without its
    OPEN landing inside it, must not be priced at that bar's close (which
    can be arbitrarily far outside the entry condition) — must report
    insufficient_data_entry instead."""
    store = _store()
    issued = _issue(store, entry_price=1.1000, atr_value=0.0050)  # entry_condition ~ [1.09950, 1.10050]
    rows = {
        T0: {"open": 1.1200, "high": 1.1200, "low": 1.0980, "close": 1.1150},  # dips through the range, opens/closes well outside it
        T0 + timedelta(minutes=30): {"open": 1.1150, "high": 1.1160, "low": 1.1140, "close": 1.1150},
    }
    candles = _bars(rows)
    window_end = datetime.fromisoformat(issued["entry_expiry_utc"])
    result = score_version(issued, [issued], candles, now=window_end + timedelta(minutes=1))
    assert result["state"] == "insufficient_data_entry", result
    assert result["assumed_entry_price"] is None, "must not invent a fill price outside the entry condition"
    print("entry price outside condition not fabricated: OK")


def test_missing_candles_at_time_exit_incomplete_coverage():
    """If candle coverage stops well before the time-exit boundary, the
    scorer must not use the last available (stale) bar's close as if it
    were the exit price at that later time."""
    store = _store()
    issued = _issue(store, entry_price=1.1000, stop=1.0950, target=1.1200, atr_value=0.0050, max_holding_time_hours=6)
    rows = {
        T0: {"open": 1.1000, "high": 1.1002, "low": 1.0998, "close": 1.1000},  # confirms entry
        T0 + timedelta(hours=1): {"open": 1.1000, "high": 1.1005, "low": 1.0995, "close": 1.1000},
        # coverage stops here — nothing anywhere near max_exit_time (T0 + 6h)
    }
    candles = _bars(rows)
    now = T0 + timedelta(hours=8)
    result = score_version(issued, [issued], candles, now=now)
    assert result["state"] == "incomplete_coverage", result
    assert result["exit_price"] is None
    print("missing candles at time exit -> incomplete_coverage, not a stale price: OK")


def test_fetch_error_for_one_pair_does_not_block_others():
    store = _store()
    v_eur = _issue(store, pair="EURUSD")
    v_gbp = _issue(store, pair="GBPUSD", entry_price=1.3000, stop=1.2950, target=1.3075)

    good_candles = _bars({T0: {"open": 1.3000, "high": 1.3002, "low": 1.2998, "close": 1.3000}})
    scored = score_all([v_eur, v_gbp], {"GBPUSD": good_candles}, now=T0 + timedelta(hours=1),
                        fetch_errors={"EURUSD": "OANDA 503"})
    by_pair = {r["pair"]: r for r in scored}
    assert by_pair["EURUSD"]["state"] == "fetch_error", by_pair["EURUSD"]
    assert by_pair["GBPUSD"]["state"] not in ("fetch_error", "no_data"), by_pair["GBPUSD"]
    print("one pair's fetch error does not block another pair's scoring: OK")


def test_coverage_window_grows_with_history_beyond_60_days():
    """A version issued well over 60 days after the pair's first-ever alert
    must still get a window sized from ITS OWN publication time, not one
    capped relative to the first alert — exercised at the
    fetch_candles_for_events level via a lightweight monkeypatch (no real
    OANDA call)."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    import alert_performance_scorer as aps

    store = _store()
    first = _issue(store, pair="EURUSD", published_at=T0, calculated_at=T0)
    much_later = T0 + timedelta(days=70)
    late = _issue(store, pair="EURUSD", published_at=much_later, calculated_at=much_later,
                  entry_price=1.1200, stop=1.1150, target=1.1275)

    requested_ranges = []

    def fake_fetch(pair, granularity, from_time, to_time, count=None):
        requested_ranges.append((from_time, to_time))
        return _bars({from_time: {"open": 1.1200, "high": 1.1200, "low": 1.1200, "close": 1.1200}})

    original = aps.fetch_oanda_candles
    aps.fetch_oanda_candles = fake_fetch
    try:
        events = store.read_all()
        candles_by_pair, fetch_errors = aps.fetch_candles_for_events(events, now=much_later + timedelta(hours=40))
    finally:
        aps.fetch_oanda_candles = original

    assert not fetch_errors
    latest_end_requested = max(end for _, end in requested_ranges)
    assert latest_end_requested >= late["recorded_at_utc"] if isinstance(late["recorded_at_utc"], datetime) else \
        latest_end_requested >= datetime.fromisoformat(late["entry_expiry_utc"]), \
        f"coverage window did not extend to cover the version issued 70 days later: {requested_ranges}"
    print("coverage window grows with history beyond 60 days: OK")


if __name__ == "__main__":
    test_expiry_boundary_bar_excluded()
    test_intrabar_stop_and_target_ambiguous()
    test_entry_price_outside_condition_not_fabricated()
    test_missing_candles_at_time_exit_incomplete_coverage()
    test_fetch_error_for_one_pair_does_not_block_others()
    test_coverage_window_grows_with_history_beyond_60_days()
    print("All alert-scorer edge-case tests passed.")
