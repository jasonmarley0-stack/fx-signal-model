"""Tests for the new prospective alert lifecycle (alert_lifecycle.py,
alert_scorer.py, alert_performance_view.py). Plain assert + __main__
runner, matching tests/test_sessions_and_orb.py's existing convention (no
pytest dependency in this repo).

Fixtures reuse two real cases surfaced by the read-only Codex audit:
  - the NZDUSD short/high duplicate-key group from 2026-08-25 (real entry/
    SL/TP drifting between polls — see CODEX_FOLLOWUP_FINDINGS.md item 5),
    used here to show the NEW system turns a real, meaningful drift into a
    linked revision instead of silently colliding two different instances.
  - the GBPJPY/Tokyo case from 2026-09-08 whose session had already closed
    hours before the signal fired (CODEX_FOLLOWUP_FINDINGS.md item 1), used
    to prove the new guarded_entry_expiry() does not reproduce that bug.
"""
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from alert_lifecycle import (  # noqa: E402
    AlertLifecycleStore, classify_and_record, guarded_entry_expiry,
)
from alert_scorer import group_lineages, effective_entry_window_end, score_version, score_all  # noqa: E402
from alert_performance_view import build_alert_performance_view, legacy_signal_as_lifecycle_view  # noqa: E402
from sessions import signal_window  # noqa: E402

T0 = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


def _store() -> AlertLifecycleStore:
    tmp = Path(tempfile.mkdtemp()) / "alert_lifecycle_log.jsonl"
    return AlertLifecycleStore(tmp)


def _issue(store, **overrides):
    base = dict(
        scanner_version="TEST_v1", pair="EURUSD", direction="long", confidence="medium",
        combined_score=0.5, entry_price=1.1000, atr_value=0.0050, stop=1.0950, target=1.1075,
        technical_inputs={"orb": 0.0, "trend": 1.0, "pattern": 0.5, "composite": 0.5},
        pestle_inputs=None, pestle_used=False, reason="test fixture",
        calculated_at=T0, published_at=T0,
    )
    base.update(overrides)
    return classify_and_record(store, **base)


def _flat_candles(start: datetime, hours: int, price: float, step_minutes: int = 30) -> pd.DataFrame:
    idx = pd.date_range(start, periods=int(hours * 60 / step_minutes), freq=f"{step_minutes}min", tz="UTC")
    return pd.DataFrame({"open": price, "high": price, "low": price, "close": price}, index=idx)


def _set_bar(df: pd.DataFrame, ts: datetime, o=None, h=None, l=None, c=None) -> pd.DataFrame:
    ts = pd.Timestamp(ts)
    nearest = df.index[df.index.get_indexer([ts], method="nearest")[0]]
    if o is not None: df.loc[nearest, "open"] = o
    if h is not None: df.loc[nearest, "high"] = h
    if l is not None: df.loc[nearest, "low"] = l
    if c is not None: df.loc[nearest, "close"] = c
    return df


def test_immutable_original_levels():
    store = _store()
    issued = _issue(store)
    assert issued["event_type"] == "issued"
    assert issued["entry_price"] == 1.1000

    _issue(store, entry_price=1.1030, stop=1.0975, target=1.1110,
           combined_score=0.55, published_at=T0 + timedelta(minutes=45))

    events = store.read_all()
    original_on_disk = next(e for e in events if e["alert_id"] == issued["alert_id"])
    assert original_on_disk["entry_price"] == 1.1000
    assert original_on_disk["stop"] == 1.0950
    assert original_on_disk["target"] == 1.1075
    print("immutable original levels: OK")


def test_linked_revision_real_duplicate_group_fixture():
    """Real NZDUSD short/high values from the same dedup-key group the
    Codex audit found — entry drifted 0.595095 -> 0.595065 a minute later,
    a real, meaningful move relative to that instance's own ATR (~7.5% of
    ATR, above the cosmetic-refresh tolerance)."""
    store = _store()
    t_first = datetime(2026, 8, 25, 8, 2, 52, tzinfo=timezone.utc)
    t_second = datetime(2026, 8, 25, 8, 3, 52, tzinfo=timezone.utc)
    atr = 0.0004025  # sl_near(0.000322) / 0.8, this instance's regime-A stop formula
    issued = _issue(store, pair="NZDUSD", direction="short", confidence="high",
                    entry_price=0.595095, atr_value=atr, stop=0.595417006798231, target=0.5944912372533168,
                    calculated_at=t_first, published_at=t_first)
    revised = _issue(store, pair="NZDUSD", direction="short", confidence="high",
                      entry_price=0.595065, atr_value=atr, stop=0.5953884353696596, target=0.5944585586818882,
                      calculated_at=t_second, published_at=t_second)
    assert revised is not None and revised["event_type"] == "revised"
    assert revised["revises_alert_id"] == issued["alert_id"]
    assert revised["lineage_id"] == issued["lineage_id"]
    print("linked revision (real duplicate-group fixture): OK")


def test_repeated_polls_no_spurious_revision():
    store = _store()
    issued = _issue(store)
    for i in range(1, 5):
        # tiny drift, well within tolerance (0.01x ATR) — simulates repeated
        # polls / confidence flicker on an otherwise-unchanged setup
        result = _issue(store, entry_price=1.1000 + 0.00001 * i, published_at=T0 + timedelta(minutes=10 * i))
        assert result is None, f"poll {i} should be a cosmetic no-op, got {result}"
    events = store.read_all()
    assert len(events) == 1, f"expected only the original issued event, got {len(events)}"
    print("repeated polls produce no spurious revision: OK")


def test_cancellation():
    store = _store()
    issued = _issue(store)
    cancelled = _issue(store, direction="no_trade", published_at=T0 + timedelta(hours=1))
    assert cancelled["event_type"] == "cancelled"
    assert cancelled["cancelled_alert_id"] == issued["alert_id"]
    assert store.active_version_for_pair("EURUSD") is None
    print("cancellation: OK")


def test_expiry_without_entry():
    store = _store()
    issued = _issue(store, entry_price=1.1000, stop=1.0950, target=1.1075)
    window_end = datetime.fromisoformat(issued["entry_expiry_utc"])
    candles = _flat_candles(T0, hours=3, price=1.2000)  # far from entry range for the whole window, never enters
    lineage = [issued]
    result = score_version(issued, lineage, candles, now=window_end + timedelta(minutes=1))
    assert result["state"] == "expired_no_entry", result
    print("expiry without entry: OK")


def test_time_exit():
    store = _store()
    issued = _issue(store, entry_price=1.1000, stop=1.0950, target=1.1075, max_holding_time_hours=6)
    candles = _flat_candles(T0, hours=8, price=1.1000)  # enters immediately, then goes nowhere — hits neither level
    now = T0 + timedelta(hours=8)
    lineage = [issued]
    result = score_version(issued, lineage, candles, now=now)
    assert result["state"] == "time_exited", result
    assert result["assumed_entry_time_utc"] is not None
    assert result["exit_price"] == 1.1000
    print("time exit: OK")


def test_scorer_attribution_to_correct_alert_version():
    store = _store()
    t1 = T0 + timedelta(minutes=45)  # well within v1's 90-minute entry window, so it's still active when v2 polls
    v1 = _issue(store, entry_price=1.1000, stop=1.0950, target=1.1075, atr_value=0.0050)
    v2 = _issue(store, entry_price=1.1030, stop=1.0975, target=1.1110, atr_value=0.0050, published_at=t1)
    assert v2["event_type"] == "revised"

    candles = _flat_candles(T0, hours=10, price=1.1000)
    # v1 enters immediately and hits ITS OWN target shortly after, well before t1
    _set_bar(candles, T0 + timedelta(minutes=30), h=1.1080, l=1.0995, c=1.1076)
    lineage = [v1, v2]
    result_v1 = score_version(v1, lineage, candles, now=t1)
    assert result_v1["state"] == "targeted", result_v1
    assert result_v1["exit_price"] == 1.1075, "must score against v1's OWN target, not v2's 1.1110"
    print("scorer attribution to correct alert version: OK")


def test_dashboard_separation_of_legacy_vs_current():
    current = [
        {"scanner_version": "H4_majors_baseline_v1", "state": "open", "r_multiple": None, "assumed_entry_time_utc": None},
        {"scanner_version": "H4_majors_baseline_v1", "state": "targeted", "r_multiple": 1.0, "assumed_entry_time_utc": "x"},
        {"scanner_version": "H4_majors_baseline_v1", "state": "stopped", "r_multiple": -1.0, "assumed_entry_time_utc": "x"},
    ]
    legacy_raw = {"pair": "EURUSD", "direction": "long", "confidence": "medium", "logged_at": "2026-08-25T10:00:00+00:00"}
    legacy_scored = {"outcome": "target", "r_multiple": 1.5}
    legacy_view = legacy_signal_as_lifecycle_view(legacy_raw, legacy_scored)
    assert legacy_view["alert_id"] is None
    assert legacy_view["bid_ask_observed"] == "not_recorded"

    view = build_alert_performance_view(current + [{**legacy_view, "r_multiple": legacy_view["r_multiple"]}])
    assert "H4_majors_baseline_v1" in view
    assert view["H4_majors_baseline_v1"]["issued"] == 3, view["H4_majors_baseline_v1"]
    assert "not_recorded" in view  # legacy rows keep their own scanner_version bucket, never merged into H4's
    assert view["not_recorded"]["issued"] == 1
    print("dashboard separation of legacy vs current: OK")


def test_session_close_already_past():
    """Real GBPJPY/Tokyo case from CODEX_FOLLOWUP_FINDINGS.md item 1: a
    signal firing at 21:30 UTC when Tokyo's session for that same UTC date
    already closed at 09:00 UTC. The OLD sessions.signal_window() reproduces
    the bug; the NEW guarded_entry_expiry() must not."""
    published_at = datetime(2026, 9, 8, 21, 30, 0, tzinfo=timezone.utc)

    old_window = signal_window("GBPJPY", published_at)
    old_valid_until = old_window["valid_until_utc"]
    assert old_valid_until < published_at, "expected the OLD bug to reproduce here (sanity-checking the fixture)"

    new_expiry = guarded_entry_expiry("GBPJPY", published_at)
    assert new_expiry > published_at, f"new entry_expiry_utc must not be in the past, got {new_expiry}"
    assert new_expiry == published_at + timedelta(minutes=90)
    print("session-close-already-past: OK (old bug reproduced as expected; new guard fixes it)")


if __name__ == "__main__":
    test_immutable_original_levels()
    test_linked_revision_real_duplicate_group_fixture()
    test_repeated_polls_no_spurious_revision()
    test_cancellation()
    test_expiry_without_entry()
    test_time_exit()
    test_scorer_attribution_to_correct_alert_version()
    test_dashboard_separation_of_legacy_vs_current()
    test_session_close_already_past()
    print("All alert-lifecycle tests passed.")
