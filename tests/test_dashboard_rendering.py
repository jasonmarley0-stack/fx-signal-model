"""Dashboard-rendering tests for the new alert feed schema, per Codex's
review of 03768e4: dashboard_server.py's alert_card_html() was still
written for the OLD push_alert() shape (stop_loss_range/take_profit_range,
direction-only branching, no alert_id/event_type). A cancellation rendered
through that old code would show the CANCELLED alert's own direction as if
it were a fresh long/short instruction, and would print an entry price
that no longer exists on a cancellation event. These tests render real
feed entries produced by alert_feed_publisher.record_and_publish (the
actual scanner-to-feed path) through the actual card renderer and check
the produced HTML, not a hand-built fixture dict.

Local-environment note: importing dashboard_server.py needs the
`eval_type_backport` package on Python < 3.10 (FastAPI/pydantic's own
requirement for evaluating `X | None` style annotations) — installed into
this repo's .venv for testing; not a repository code change.
"""
import sys
import json
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))
from alert_lifecycle import AlertLifecycleStore  # noqa: E402
from alert_feed_publisher import record_and_publish  # noqa: E402
import dashboard_server as ds  # noqa: E402

T0 = datetime(2026, 9, 1, 8, 0, 0, tzinfo=timezone.utc)


def _fixtures():
    tmp = Path(tempfile.mkdtemp())
    store = AlertLifecycleStore(tmp / "alert_lifecycle_log.jsonl")
    alerts_path = tmp / "alerts.json"
    return store, alerts_path


def _feed_alerts(alerts_path: Path) -> list[dict]:
    return json.loads(alerts_path.read_text()).get("alerts", [])


def test_render_issued_card():
    store, alerts_path = _fixtures()
    record_and_publish(store, alerts_path, {"orb": 0.0, "trend": 1.0, "pattern": 0.5, "composite": 0.5}, {},
                        scanner_version="TEST_v1", pair="EURUSD", direction="long", confidence="medium",
                        combined_score=0.5, entry_price=1.1000, atr_value=0.0050, stop=1.0950, target=1.1075,
                        technical_inputs={"orb": 0.0, "trend": 1.0, "pattern": 0.5, "composite": 0.5},
                        pestle_inputs=None, pestle_used=False, reason="test", calculated_at=T0, published_at=T0)
    alert = _feed_alerts(alerts_path)[0]
    html = ds.alert_card_html(alert)

    assert "1.10000" in html  # entry
    assert "1.09500" in html  # stop
    assert "1.10750" in html  # target
    assert "Enter by" in html  # entry expiry rendered
    assert 'class="badge long">Long</span>' in html
    assert "dir-long" in html
    assert "cancelled" not in html.lower()
    assert "Updated" not in html
    print("issued card renders entry/stop/target/expiry: OK")


def test_render_revised_card_shows_original_levels_in_guidance():
    store, alerts_path = _fixtures()
    kwargs = dict(scanner_version="TEST_v1", pair="EURUSD", direction="long", confidence="medium",
                  combined_score=0.5, atr_value=0.0050, technical_inputs={}, pestle_inputs=None, pestle_used=False,
                  reason="test")
    record_and_publish(store, alerts_path, {}, {}, entry_price=1.1000, stop=1.0950, target=1.1075,
                        calculated_at=T0, published_at=T0, **kwargs)
    t1 = T0 + timedelta(minutes=40)
    record_and_publish(store, alerts_path, {}, {}, entry_price=1.1030, stop=1.0975, target=1.1110,
                        calculated_at=t1, published_at=t1, **kwargs)
    revised_alert = _feed_alerts(alerts_path)[-1]
    html = ds.alert_card_html(revised_alert)

    assert 'class="badge updated"' in html
    assert "Updated" in html
    assert "Revises alert" in html
    assert "1.10300" in html  # the NEW entry (revision's own level)
    assert "1.09500" in html  # the ORIGINAL stop, in the existing-position guidance
    assert "1.10750" in html  # the ORIGINAL target, in the existing-position guidance
    assert "existing-position-note" in html
    assert "already entered" in html
    print("revised card shows updated badge, new levels, and original-position guidance: OK")


def test_render_cancelled_card_has_no_entry_price_and_no_direction_badge():
    store, alerts_path = _fixtures()
    kwargs = dict(scanner_version="TEST_v1", pair="EURUSD", confidence="medium", combined_score=0.5,
                  atr_value=0.0050, technical_inputs={}, pestle_inputs=None, pestle_used=False, reason="test")
    record_and_publish(store, alerts_path, {}, {}, direction="long", entry_price=1.1000, stop=1.0950,
                        target=1.1075, calculated_at=T0, published_at=T0, **kwargs)
    t1 = T0 + timedelta(minutes=20)
    record_and_publish(store, alerts_path, {}, {}, direction="no_trade", entry_price=1.1000, stop=None,
                        target=None, calculated_at=t1, published_at=t1, **kwargs)
    cancelled_alert = _feed_alerts(alerts_path)[-1]
    html = ds.alert_card_html(cancelled_alert)

    assert 'class="badge cancelled">Cancelled</span>' in html
    assert 'class="badge long"' not in html and 'class="badge short"' not in html, \
        "a cancellation card must never carry a long/short instruction badge"
    assert "entry mono" not in html, "a cancellation card must not render an entry-price field at all"
    assert "No entry price applies" in html
    assert "existing-position-note" in html
    print("cancelled card has no entry price and no long/short badge: OK")


def test_render_reversal_both_cards_distinct():
    """On a reversal, the feed carries BOTH a cancellation of the old
    lineage and a fresh issuance for the new direction — render each and
    confirm the cancellation card never looks like the new instruction."""
    store, alerts_path = _fixtures()
    kwargs = dict(scanner_version="TEST_v1", pair="EURUSD", confidence="medium", combined_score=0.5,
                  atr_value=0.0050, technical_inputs={}, pestle_inputs=None, pestle_used=False, reason="test")
    record_and_publish(store, alerts_path, {}, {}, direction="long", entry_price=1.1000, stop=1.0950,
                        target=1.1075, calculated_at=T0, published_at=T0, **kwargs)
    t1 = T0 + timedelta(minutes=20)
    record_and_publish(store, alerts_path, {}, {}, direction="short", entry_price=1.0980, stop=1.1030,
                        target=1.0905, calculated_at=t1, published_at=t1, **kwargs)
    feed = _feed_alerts(alerts_path)
    assert len(feed) == 3
    cancel_html = ds.alert_card_html(feed[1])
    new_html = ds.alert_card_html(feed[2])

    assert 'class="badge cancelled">Cancelled</span>' in cancel_html
    assert 'class="badge short"' not in cancel_html, "the cancellation of the OLD long must not itself carry a short badge"
    assert "entry mono" not in cancel_html

    assert 'class="badge short">Short</span>' in new_html
    assert "cancelled" not in new_html.lower()
    assert "1.09800" in new_html  # the new short's own entry
    print("reversal renders a distinct cancellation card and a distinct new-direction card: OK")


def test_notification_js_wording_present_for_cancel_and_revise():
    page = ds.render_page(None, None, {"alerts": []})
    assert "a.event_type === 'cancelled'" in page
    assert "— Cancelled" in page
    assert "a.event_type === 'revised'" in page
    assert "Updated" in page
    print("notification JS branches on event_type for cancelled/revised wording: OK")


if __name__ == "__main__":
    test_render_issued_card()
    test_render_revised_card_shows_original_levels_in_guidance()
    test_render_cancelled_card_has_no_entry_price_and_no_direction_badge()
    test_render_reversal_both_cards_distinct()
    test_notification_js_wording_present_for_cancel_and_revise()
    print("All dashboard-rendering tests passed.")
