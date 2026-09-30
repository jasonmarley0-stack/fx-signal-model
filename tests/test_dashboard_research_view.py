"""Tests for the Research view added to dashboard_server.py: auth
protection, explicit states for missing/malformed/stale snapshots,
denominator display, suppressed-trade handling, and that the pre-existing
Live/Performance views/routes are unaffected. Uses FastAPI's TestClient --
no real network, no real observer checkout, no systemd. Every
research_snapshot_publisher.py-shaped payload used here is a literal
fixture dict, never live-computed.
"""
from __future__ import annotations
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ["DASHBOARD_USER"] = "testuser"
os.environ["DASHBOARD_PASSWORD"] = "testpass123"

import dashboard_server as ds  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

AUTH = ("testuser", "testpass123")

HEALTH_OK = {
    "generated_at_utc": "2026-09-30T20:54:54.724798+00:00", "state": "ok", "error": None,
    "run_identity": {"run_id": "885c5e3e-test", "started_at_utc": "2026-09-28T17:54:50+00:00",
                      "source_hash": "84ec2b09" * 8, "elapsed_days": 2.1},
    "service": {"name": "prospective-baseline-observer.service", "status": "active", "checked_via": "systemctl"},
    "quotes": {"latest_receipt_utc": "2026-09-30T20:54:50+00:00", "latest_receipt_age_seconds": 4.0,
               "pairs": {"EURUSD": {"latest_receipt_utc": "2026-09-30T20:54:50+00:00", "latest_receipt_age_seconds": 4.0,
                                     "samples_in_window": 60, "valid_samples_in_window": 60, "has_gap": False}},
               "pair_coverage_count": 1, "pair_coverage_total": 7, "invalid_or_stale_quotes_in_window": 0,
               "total_quotes_in_window": 60, "window_seconds": 900},
    "recording_failures": {"poll_failed": 0, "quote_poll_failed": 0, "crashed": 0, "total": 0},
    "recording_health": "healthy",
}

KPI_OK = {
    "generated_at_utc": "2026-09-30T20:55:04.202639+00:00", "state": "ok", "error": None, "run_id": "885c5e3e-test",
    "kpi": {
        "counts": {"eligible_alerts": 2, "suppressed_existing_position": 1, "entered": 1, "completed": 1,
                   "pending_open": 0, "unknown_total": 0, "missed_entries_confirmed_zero_pnl": 1, "no_signal_ticks": 3},
        "avg_net_r_per_completed_trade": {"value": 1.5, "denominator": 1},
        "avg_net_r_per_all_eligible_alert": {"value": 0.75, "denominator": 2, "is_undetermined": False, "reason": None},
        "max_drawdown_r_partial_completed_trades_only": 0.0,
        "equity_curve": [{"exit_time_utc": "2026-09-29T10:00:00+00:00", "pair": "EURUSD", "r_multiple": 1.5,
                           "cumulative_r": 1.5, "drawdown_r": 0.0}],
        "by_month": {}, "operational": {}, "unobserved": ["Financing/swap not observed."],
    },
    "ledger": [
        {"pair": "EURUSD", "direction": "long", "entry_condition_lo": 1.0995, "entry_condition_hi": 1.1005,
         "stop": 1.098, "target": 1.102, "assumed_entry_time_utc": "2026-09-29T09:00:05+00:00",
         "assumed_entry_price": 1.1001, "exit_time_utc": "2026-09-29T10:00:00+00:00", "exit_price": 1.1021,
         "state": "targeted", "r_multiple": 1.5, "executable": True, "same_sample_exit": False,
         "source_candle_completion_utc": "2026-09-29T09:00:00+00:00", "actual_recording_time_utc": "2026-09-29T09:00:05+00:00",
         "decision_delay_seconds": 1.1, "entry_expiry_utc": "2026-09-29T13:00:00+00:00", "max_holding_time_hours": 30.0,
         "scheduled_exit_time_utc": None, "execution_delay_seconds": None, "caveats": ["note"]},
        {"pair": "GBPUSD", "direction": "short", "actual_recording_time_utc": "2026-09-29T09:30:00+00:00",
         "executable": False, "state": "suppressed_existing_position", "suppressed_until_utc": "2026-09-29T13:00:00+00:00"},
    ],
}

KPI_UNDETERMINED = {
    **KPI_OK,
    "kpi": {**KPI_OK["kpi"], "avg_net_r_per_all_eligible_alert": {
        "value": None, "denominator": 2, "is_undetermined": True, "reason": "1 of 2 eligible alerts have an unknown outcome"}},
}


def _client_with_snapshots(health: dict | None, kpi: dict | None, health_malformed: bool = False) -> TestClient:
    """Points dashboard_server's snapshot path constants at a fresh temp
    dir and writes the given fixture payloads there (or leaves a file
    missing entirely when the payload is None) -- never touches the real
    research_snapshots/ directory this repo uses for local dev."""
    tmp = Path(tempfile.mkdtemp())
    ds.RESEARCH_HEALTH_PATH = tmp / "research_health.json"
    ds.RESEARCH_KPI_PATH = tmp / "research_kpi.json"
    if health_malformed:
        ds.RESEARCH_HEALTH_PATH.write_text("{not valid json")
    elif health is not None:
        ds.RESEARCH_HEALTH_PATH.write_text(json.dumps(health))
    if kpi is not None:
        ds.RESEARCH_KPI_PATH.write_text(json.dumps(kpi))
    return TestClient(ds.app)


def test_research_routes_require_auth():
    client = _client_with_snapshots(HEALTH_OK, KPI_OK)
    assert client.get("/").status_code == 401
    assert client.get("/research/health").status_code == 401
    assert client.get("/research/kpi").status_code == 401
    print("dashboard: / and /research/* all require auth: OK")


def test_research_routes_return_exact_snapshot_contents():
    client = _client_with_snapshots(HEALTH_OK, KPI_OK)
    r = client.get("/research/health", auth=AUTH)
    assert r.status_code == 200
    assert r.json() == HEALTH_OK
    r = client.get("/research/kpi", auth=AUTH)
    assert r.status_code == 200
    assert r.json() == KPI_OK
    print("dashboard: /research/health and /research/kpi return the published snapshot as-is: OK")


def test_missing_snapshot_files_are_explicit_not_a_crash():
    client = _client_with_snapshots(None, None)
    r = client.get("/research/health", auth=AUTH)
    assert r.status_code == 200
    assert r.json()["state"] == "not_published"
    r = client.get("/research/kpi", auth=AUTH)
    assert r.status_code == 200
    assert r.json()["state"] == "not_published"
    # full page load must also succeed with no snapshots published yet
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200
    assert "not_published" in r.text
    print("dashboard: missing snapshot files -> explicit not_published state, page still loads: OK")


def test_malformed_snapshot_file_is_explicit_not_a_crash():
    client = _client_with_snapshots(None, KPI_OK, health_malformed=True)
    r = client.get("/research/health", auth=AUTH)
    assert r.status_code == 200
    assert r.json()["state"] == "malformed_snapshot_file"
    print("dashboard: malformed snapshot file -> explicit malformed_snapshot_file state, no crash: OK")


def test_denominator_always_shown_including_when_undetermined():
    client = _client_with_snapshots(HEALTH_OK, KPI_UNDETERMINED)
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200
    # the embedded JSON payload carries the denominator even when undetermined -- never silently dropped
    assert '"denominator": 2' in r.text
    assert '"is_undetermined": true' in r.text
    print("dashboard: all-eligible-alert denominator is present in the page even when undetermined: OK")


def test_suppressed_trades_present_and_distinct_from_executable_ledger():
    client = _client_with_snapshots(HEALTH_OK, KPI_OK)
    r = client.get("/research/kpi", auth=AUTH)
    ledger = r.json()["ledger"]
    executable = [row for row in ledger if row["executable"]]
    suppressed = [row for row in ledger if not row["executable"]]
    assert len(executable) == 1 and executable[0]["pair"] == "EURUSD"
    assert len(suppressed) == 1 and suppressed[0]["pair"] == "GBPUSD"
    assert suppressed[0]["state"] == "suppressed_existing_position"
    print("dashboard: suppressed decisions are present and distinguishable from executable ledger rows: OK")


def test_manifest_mismatch_state_rendered_without_crash_or_leaked_path():
    payload = {"generated_at_utc": "2026-09-30T21:00:00+00:00", "state": "manifest_mismatch",
               "error": "Source/contract hash no longer matches this run's recorded manifest.",
               "run_id": "885c5e3e-test", "kpi": None, "ledger": None}
    client = _client_with_snapshots(HEALTH_OK, payload)
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200
    assert "/root/" not in r.text and "/tmp/" not in r.text
    print("dashboard: manifest_mismatch state renders without crashing and without leaking a filesystem path: OK")


def test_credentials_never_appear_in_page_source():
    client = _client_with_snapshots(HEALTH_OK, KPI_OK)
    r = client.get("/", auth=AUTH)
    assert os.environ["DASHBOARD_PASSWORD"] not in r.text
    print("dashboard: the dashboard password never appears in the rendered page: OK")


def test_existing_live_and_performance_views_unaffected():
    """Regression check: the Research addition must not change Live's or
    Performance's own behaviour, including with no live_scan.json/
    performance.json/alerts.json present (their existing empty states)."""
    client = _client_with_snapshots(HEALTH_OK, KPI_OK)
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200
    assert "No signals have fired yet" in r.text  # existing Live empty-state copy, unchanged
    assert "No performance data yet" in r.text     # existing Performance empty-state copy, unchanged
    assert client.get("/table", auth=AUTH).status_code == 200
    assert client.get("/feed", auth=AUTH).status_code == 200
    assert client.get("/alerts", auth=AUTH).status_code == 200
    assert client.get("/api/performance", auth=AUTH).status_code == 200
    assert client.get("/api/health", auth=AUTH).status_code == 200
    print("dashboard: existing Live/Performance views and routes are unaffected: OK")


def test_no_positive_styling_class_for_undetermined_value():
    """Product requirement: never show positive-performance (green 'pos')
    styling for a null/undetermined value. The undetermined case must use
    the neutral 'undetermined' class, not 'pos'."""
    client = _client_with_snapshots(HEALTH_OK, KPI_UNDETERMINED)
    r = client.get("/", auth=AUTH)
    assert "value ${{allEligible.is_undetermined ? 'undetermined'" not in r.text  # sanity: template rendered, not left literal
    assert "'undetermined' : (allEligible.value" in r.text or "allEligible.is_undetermined ? 'undetermined'" in r.text
    print("dashboard: the undetermined-value tile uses the neutral class, never positive-performance styling: OK")


if __name__ == "__main__":
    test_research_routes_require_auth()
    test_research_routes_return_exact_snapshot_contents()
    test_missing_snapshot_files_are_explicit_not_a_crash()
    test_malformed_snapshot_file_is_explicit_not_a_crash()
    test_denominator_always_shown_including_when_undetermined()
    test_suppressed_trades_present_and_distinct_from_executable_ledger()
    test_manifest_mismatch_state_rendered_without_crash_or_leaked_path()
    test_credentials_never_appear_in_page_source()
    test_existing_live_and_performance_views_unaffected()
    test_no_positive_styling_class_for_undetermined_value()
    print("All dashboard Research-view tests passed (no network, no real observer checkout).")
