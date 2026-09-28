"""Unifies alert_lifecycle_log.jsonl recording with the actual user-facing
feed (alerts.json — what dashboard_server.py's Live tab and subscriber
notifications are built from). Core acceptance rule this exists for: "A
recorded revision or cancellation cannot exist only in
alert_lifecycle_log.jsonl."

record_and_publish() is the one function live_scanner.py calls. It
persists via alert_lifecycle.classify_and_record(), then publishes EVERY
resulting event (there can be two, on a direction reversal — see
classify_and_record's docstring) to the SAME feed push_alert() used to
write, carrying the SAME alert_id/lineage_id/event_type/timestamp/
immutable levels the lifecycle log has — not a second, independently
computed representation that could drift from it. A publish failure is
recorded (status='failed', with the error) rather than silently treated as
delivered, and is recoverable via retry_unpublished().
"""
from __future__ import annotations
import json
from pathlib import Path

from alert_lifecycle import AlertLifecycleStore, classify_and_record

MAX_ALERTS = 50


def _existing_position_message(existing_position: dict | None) -> str | None:
    """Requirement 4: a revision/cancellation must state what happens to a
    subscriber who already entered under the version being superseded —
    not just link to it. The scorer (alert_scorer.py) backs this up by
    never truncating a version's post-entry stop/target/time-exit scan at
    a later revision's timestamp."""
    if not existing_position:
        return None
    return (
        f"If you already entered alert {existing_position['alert_id']}, that trade is unaffected by this update — "
        f"continue to follow its ORIGINAL stop {existing_position['original_stop']:.5f} and "
        f"target {existing_position['original_target']:.5f}, with a time exit "
        f"{existing_position['original_max_holding_time_hours']:g}h after your entry if neither is hit first. "
        f"This only changes what is offered for a NEW entry from now on."
    )


def _feed_entry_for_event(event: dict, tech: dict, pestle: dict) -> dict:
    event_type = event["event_type"]
    pair, direction = event["pair"], event["direction"]
    confidence = event.get("confidence")
    existing_position_guidance = _existing_position_message(event.get("existing_position"))

    if event_type == "cancelled":
        message = f"{pair}: alert {event['cancelled_alert_id']} cancelled — {event['cancellation_reason']}"
    elif event_type == "revised":
        message = (f"{pair}: UPDATED — {direction.upper()} ({confidence}) @ {event['entry_price']:.5f} "
                    f"(revises alert {event['revises_alert_id']})")
    else:
        message = f"{pair}: {direction.upper()} ({confidence}) @ {event['entry_price']:.5f}"

    return {
        "id": event["event_id"], "alert_id": event["alert_id"], "lineage_id": event["lineage_id"],
        "event_type": event_type, "time": event["recorded_at_utc"],
        "pair": pair, "direction": direction, "confidence": confidence,
        "entry": event.get("entry_price"), "stop": event.get("stop"), "target": event.get("target"),
        "entry_expiry_utc": event.get("entry_expiry_utc"),
        "revises_alert_id": event.get("revises_alert_id"), "cancelled_alert_id": event.get("cancelled_alert_id"),
        "message": message,
        "existing_position_guidance": existing_position_guidance,
        "reason": event.get("reason") or event.get("cancellation_reason") or event.get("revision_reason"),
        "tech": tech, "pestle": pestle,
    }


def publish_to_feed(alerts_path: Path, event: dict, tech: dict, pestle: dict) -> None:
    """Raises on failure — record_and_publish() is responsible for catching
    this and recording a feed_publish_result, so a failure here is visible
    and recoverable rather than silently swallowed."""
    alerts_path = Path(alerts_path)
    alerts = []
    if alerts_path.exists():
        try:
            alerts = json.loads(alerts_path.read_text()).get("alerts", [])
        except json.JSONDecodeError:
            alerts = []
    alerts.append(_feed_entry_for_event(event, tech, pestle))
    alerts = alerts[-MAX_ALERTS:]
    tmp = alerts_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"alerts": alerts}, indent=2))
    tmp.replace(alerts_path)


def record_and_publish(store: AlertLifecycleStore, alerts_path: Path, tech: dict, pestle: dict, **classify_kwargs) -> list[dict]:
    """The single call site live_scanner.py uses in place of the old,
    independent record_alert_lifecycle() + push_alert() pair. Persists via
    classify_and_record, then publishes every resulting event to the feed,
    recording a feed_publish_result for each — so persistence succeeding
    is never mistaken for a subscriber having been notified."""
    events = classify_and_record(store, **classify_kwargs)
    for event in events:
        try:
            publish_to_feed(alerts_path, event, tech, pestle)
            store.record_feed_publish_result(event, status="delivered")
        except Exception as ex:  # noqa: BLE001 — must not crash the scanner's other pairs; failure is recorded, not swallowed
            store.record_feed_publish_result(event, status="failed", error=str(ex))
    return events


def retry_unpublished(store: AlertLifecycleStore, alerts_path: Path) -> list[dict]:
    """Recovery pass for events whose latest feed_publish_result isn't
    'delivered' (including ones never attempted). The original tech/pestle
    breakdown isn't recoverable after the fact, so a retried feed entry
    carries empty dicts for those — the IDs/levels/timestamps the
    acceptance rule cares about are unaffected."""
    retried = []
    for event in store.unpublished_events():
        try:
            publish_to_feed(alerts_path, event, {}, {})
            store.record_feed_publish_result(event, status="delivered")
            retried.append(event)
        except Exception as ex:  # noqa: BLE001
            store.record_feed_publish_result(event, status="failed", error=str(ex))
    return retried
