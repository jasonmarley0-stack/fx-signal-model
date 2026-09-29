"""Integration tests for the actual scanner-to-feed path (alert_feed_publisher.
record_and_publish / retry_unpublished) — not just the lifecycle classifier
in isolation. This is the exact function live_scanner.py calls on every
poll. Core acceptance rule under test: a recorded revision or cancellation
must not exist only in alert_lifecycle_log.jsonl — it must reach the
user-facing feed (alerts.json) with the SAME alert_id/lineage/event
type/timestamp/levels, and a publish failure must be visible and
recoverable rather than silently treated as delivered.
"""
import sys
import json
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from alert_lifecycle import AlertLifecycleStore, FEED_PUBLISH_STATUS_WRITTEN  # noqa: E402
import alert_feed_publisher as afp  # noqa: E402
from alert_feed_publisher import record_and_publish, retry_unpublished  # noqa: E402

T0 = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


def _fixtures():
    tmp = Path(tempfile.mkdtemp())
    store = AlertLifecycleStore(tmp / "alert_lifecycle_log.jsonl")
    alerts_path = tmp / "alerts.json"
    return store, alerts_path


def _feed_alerts(alerts_path: Path) -> list[dict]:
    if not alerts_path.exists():
        return []
    return json.loads(alerts_path.read_text()).get("alerts", [])


def test_first_issue_reaches_feed_with_matching_ids():
    store, alerts_path = _fixtures()
    events = record_and_publish(store, alerts_path, {"orb": 0.0}, {},
                                 scanner_version="TEST_v1", pair="EURUSD", direction="long", confidence="medium",
                                 combined_score=0.5, entry_price=1.1000, atr_value=0.0050, stop=1.0950, target=1.1075,
                                 technical_inputs={"orb": 0.0}, pestle_inputs=None, pestle_used=False,
                                 reason="test", calculated_at=T0, published_at=T0)
    assert len(events) == 1 and events[0]["event_type"] == "issued"
    feed = _feed_alerts(alerts_path)
    assert len(feed) == 1
    assert feed[0]["alert_id"] == events[0]["alert_id"]
    assert feed[0]["lineage_id"] == events[0]["lineage_id"]
    assert feed[0]["entry"] == events[0]["entry_price"]
    assert feed[0]["stop"] == events[0]["stop"] and feed[0]["target"] == events[0]["target"]
    assert store.feed_publish_status_by_event_id()[events[0]["event_id"]] == FEED_PUBLISH_STATUS_WRITTEN
    print("first issue reaches feed with matching IDs/levels: OK")


def test_same_direction_revision_reaches_feed_with_guidance():
    store, alerts_path = _fixtures()
    kwargs = dict(scanner_version="TEST_v1", pair="EURUSD", direction="long", confidence="medium",
                  combined_score=0.5, atr_value=0.0050, technical_inputs={}, pestle_inputs=None, pestle_used=False,
                  reason="test")
    issued = record_and_publish(store, alerts_path, {}, {}, entry_price=1.1000, stop=1.0950, target=1.1075,
                                 calculated_at=T0, published_at=T0, **kwargs)[0]
    t1 = T0 + timedelta(minutes=40)
    revised = record_and_publish(store, alerts_path, {}, {}, entry_price=1.1030, stop=1.0975, target=1.1110,
                                  calculated_at=t1, published_at=t1, **kwargs)[0]
    assert revised["event_type"] == "revised"

    feed = _feed_alerts(alerts_path)
    assert len(feed) == 2
    rev_entry = feed[-1]
    assert rev_entry["event_type"] == "revised"
    assert rev_entry["alert_id"] == revised["alert_id"]
    assert rev_entry["revises_alert_id"] == issued["alert_id"]
    assert rev_entry["existing_position_guidance"] is not None
    assert f"{issued['stop']:.5f}" in rev_entry["existing_position_guidance"]
    assert f"{issued['target']:.5f}" in rev_entry["existing_position_guidance"]
    print("same-direction revision reaches feed with already-entered guidance: OK")


def test_cancellation_reaches_feed():
    store, alerts_path = _fixtures()
    kwargs = dict(scanner_version="TEST_v1", pair="EURUSD", confidence="medium", combined_score=0.5,
                  atr_value=0.0050, technical_inputs={}, pestle_inputs=None, pestle_used=False, reason="test")
    issued = record_and_publish(store, alerts_path, {}, {}, direction="long", entry_price=1.1000,
                                 stop=1.0950, target=1.1075, calculated_at=T0, published_at=T0, **kwargs)[0]
    t1 = T0 + timedelta(minutes=20)
    cancelled = record_and_publish(store, alerts_path, {}, {}, direction="no_trade", entry_price=1.1000,
                                    stop=None, target=None, calculated_at=t1, published_at=t1, **kwargs)[0]
    assert cancelled["event_type"] == "cancelled"
    feed = _feed_alerts(alerts_path)
    assert feed[-1]["event_type"] == "cancelled"
    assert feed[-1]["cancelled_alert_id"] == issued["alert_id"]
    assert feed[-1]["existing_position_guidance"] is not None
    print("cancellation reaches feed: OK")


def test_reversal_publishes_both_cancellation_and_new_issuance():
    """The bug this specifically guards against: classify_and_record used
    to return only the LAST event on a reversal, so the cancellation of
    the old (long) lineage never reached the feed at all — only the new
    short alert did. Fixed by returning every event produced."""
    store, alerts_path = _fixtures()
    kwargs = dict(scanner_version="TEST_v1", pair="EURUSD", confidence="medium", combined_score=0.5,
                  atr_value=0.0050, technical_inputs={}, pestle_inputs=None, pestle_used=False, reason="test")
    issued = record_and_publish(store, alerts_path, {}, {}, direction="long", entry_price=1.1000,
                                 stop=1.0950, target=1.1075, calculated_at=T0, published_at=T0, **kwargs)[0]
    t1 = T0 + timedelta(minutes=20)
    events = record_and_publish(store, alerts_path, {}, {}, direction="short", entry_price=1.0980,
                                 stop=1.1030, target=1.0905, calculated_at=t1, published_at=t1, **kwargs)
    assert [e["event_type"] for e in events] == ["cancelled", "issued"], events
    feed = _feed_alerts(alerts_path)
    assert len(feed) == 3, f"expected original issue + cancellation + new issue on the feed, got {len(feed)}"
    assert feed[1]["event_type"] == "cancelled" and feed[1]["cancelled_alert_id"] == issued["alert_id"]
    assert feed[2]["event_type"] == "issued" and feed[2]["direction"] == "short"
    print("direction reversal publishes BOTH the cancellation and the new issuance: OK")


def test_no_op_poll_leaves_feed_unchanged():
    store, alerts_path = _fixtures()
    kwargs = dict(scanner_version="TEST_v1", pair="EURUSD", direction="long", confidence="medium",
                  combined_score=0.5, atr_value=0.0050, technical_inputs={}, pestle_inputs=None, pestle_used=False,
                  reason="test")
    record_and_publish(store, alerts_path, {}, {}, entry_price=1.1000, stop=1.0950, target=1.1075,
                        calculated_at=T0, published_at=T0, **kwargs)
    before = _feed_alerts(alerts_path)
    for i in range(1, 5):
        t = T0 + timedelta(minutes=10 * i)
        events = record_and_publish(store, alerts_path, {}, {}, entry_price=1.1000 + 0.00001 * i,
                                     stop=1.0950, target=1.1075, calculated_at=t, published_at=t, **kwargs)
        assert events == [], f"poll {i} should be a no-op, got {events}"
    after = _feed_alerts(alerts_path)
    assert before == after, "a repeated no-op poll must not change the feed at all"
    print("repeated no-op polls leave the feed unchanged: OK")


def test_publish_failure_is_visible_and_recoverable_without_duplication():
    store, alerts_path = _fixtures()
    kwargs = dict(scanner_version="TEST_v1", pair="EURUSD", direction="long", confidence="medium",
                  combined_score=0.5, atr_value=0.0050, technical_inputs={}, pestle_inputs=None, pestle_used=False,
                  reason="test")

    original_publish = afp.publish_to_feed

    def failing_publish(*a, **kw):
        raise RuntimeError("simulated disk failure")

    afp.publish_to_feed = failing_publish
    try:
        events = record_and_publish(store, alerts_path, {}, {}, entry_price=1.1000, stop=1.0950, target=1.1075,
                                     calculated_at=T0, published_at=T0, **kwargs)
    finally:
        afp.publish_to_feed = original_publish

    assert len(events) == 1  # the lifecycle event WAS persisted
    status = store.feed_publish_status_by_event_id()
    assert status[events[0]["event_id"]] == "failed", "a publish failure must be recorded, not silently treated as delivered"
    assert _feed_alerts(alerts_path) == [], "the feed must not show an event that failed to publish"

    retried = retry_unpublished(store, alerts_path)
    assert len(retried) == 1 and retried[0]["event_id"] == events[0]["event_id"]
    feed = _feed_alerts(alerts_path)
    assert len(feed) == 1, f"retry must publish exactly once, got {len(feed)}"
    assert feed[0]["alert_id"] == events[0]["alert_id"]
    assert store.feed_publish_status_by_event_id()[events[0]["event_id"]] == FEED_PUBLISH_STATUS_WRITTEN

    # a second retry pass must not duplicate the now-delivered event
    retried_again = retry_unpublished(store, alerts_path)
    assert retried_again == []
    assert len(_feed_alerts(alerts_path)) == 1
    print("publish failure visible, recoverable, and retry does not duplicate: OK")


if __name__ == "__main__":
    test_first_issue_reaches_feed_with_matching_ids()
    test_same_direction_revision_reaches_feed_with_guidance()
    test_cancellation_reaches_feed()
    test_reversal_publishes_both_cancellation_and_new_issuance()
    test_no_op_poll_leaves_feed_unchanged()
    test_publish_failure_is_visible_and_recoverable_without_duplication()
    print("All alert-feed integration tests passed.")
