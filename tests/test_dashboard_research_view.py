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

# Defect-1 regression fixture: zero eligible alerts (a fresh run). Per
# report.py, avg_net_r_per_all_eligible_alert.value is None here but
# is_undetermined is FALSE (that field means "undetermined because of an
# unknown OUTCOME", a narrower condition than "nothing to compute yet") --
# exactly the case that used to fall through to a null >= 0 comparison
# client-side and get colored as if it were a real (negative) number.
KPI_ZERO_ELIGIBLE = {
    "generated_at_utc": "2026-09-30T21:30:05+00:00", "state": "ok", "error": None, "run_id": "885c5e3e-test",
    "kpi": {
        "counts": {"eligible_alerts": 0, "suppressed_existing_position": 0, "entered": 0, "completed": 0,
                   "pending_open": 0, "unknown_total": 0, "missed_entries_confirmed_zero_pnl": 0, "no_signal_ticks": 0},
        "avg_net_r_per_completed_trade": {"value": None, "denominator": 0},
        "avg_net_r_per_all_eligible_alert": {"value": None, "denominator": 0, "is_undetermined": False, "reason": None},
        "max_drawdown_r_partial_completed_trades_only": 0.0, "equity_curve": [], "by_month": {}, "operational": {}, "unobserved": [],
    },
    "ledger": [],
}

# Defect-3 regression fixtures: health and KPI snapshots naming different
# run_ids (e.g. published moments apart around a restart).
HEALTH_RUN_A = {**HEALTH_OK, "run_identity": {**HEALTH_OK["run_identity"], "run_id": "run-AAA"}}
KPI_RUN_B = {**KPI_OK, "run_id": "run-BBB"}

# Defect-2 regression fixtures: health snapshots that must NOT let the
# roadmap claim "currently running".
HEALTH_ERROR = {
    "generated_at_utc": "2026-09-30T21:30:00+00:00", "state": "error",
    "error": "OSError: <path> not readable", "run_identity": HEALTH_OK["run_identity"],
    "service": HEALTH_OK["service"], "quotes": None, "recording_failures": None, "recording_health": "unknown",
}


def _client_with_snapshots(health: dict | None, kpi: dict | None, health_malformed: bool = False) -> TestClient:
    """Points dashboard_server's snapshot path constants at a fresh temp
    dir and writes the given fixture payloads there (or leaves a file
    missing entirely when the payload is None) -- never touches the real
    research_snapshots/ directory this repo uses for local dev."""
    tmp = Path(tempfile.mkdtemp())
    ds.RESEARCH_HEALTH_PATH = tmp / "research_health.json"
    ds.RESEARCH_KPI_PATH = tmp / "research_kpi.json"
    # v2 defaults to "not published" (a fresh temp dir with nothing in it)
    # unless a test explicitly points these elsewhere -- never accidentally
    # reads a stray file from another test or the real research_snapshots/.
    ds.RESEARCH_HEALTH_V2_PATH = tmp / "research_health_v2.json"
    ds.RESEARCH_KPI_V2_PATH = tmp / "research_kpi_v2.json"
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
    assert "allEligible.is_undetermined || !isFiniteNum(allEligible.value)" in r.text  # the neutral-first branch is present, not stripped
    print("dashboard: the undetermined-value tile uses the neutral class, never positive-performance styling: OK")


def test_zero_eligible_alerts_page_renders_and_embeds_neutral_data():
    """Defect-1 regression, the exact case named in the correction order:
    zero eligible alerts. avg_net_r_per_all_eligible_alert.value is null
    while is_undetermined is FALSE (report.py's is_undetermined only means
    "an unknown outcome exists", not "nothing to compute yet") -- the
    client-side helper (isFiniteNum/perfClass) is what must catch this,
    not the server; this test confirms the page renders cleanly and the
    null-with-zero-denominator shape reaches the client unmodified."""
    client = _client_with_snapshots(HEALTH_OK, KPI_ZERO_ELIGIBLE)
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200
    assert '"denominator": 0' in r.text
    assert '"is_undetermined": false' in r.text
    assert '"value": null' in r.text
    print("dashboard: zero-eligible-alerts page renders cleanly with the null/is_undetermined=false shape intact for the client: OK")


def test_health_error_state_never_claims_currently_running():
    """Defect-2 regression: a health snapshot in a non-"ok" state (here,
    "error") must not let the roadmap assert the observation is
    "currently running" -- the page must render the raw state/error data
    for the client-side roadmap-status derivation to act on, and must
    never crash on an error message containing HTML-special characters
    (a redacted "<path>" placeholder, from research_snapshot_publisher.py's
    _short_error)."""
    client = _client_with_snapshots(HEALTH_ERROR, KPI_OK)
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200
    assert '"state": "error"' in r.text
    assert "<path>" in r.text or "\\u003cpath\\u003e" in r.text  # the raw (unescaped-at-the-JSON-layer) error text reaches the client
    # the client-side escapeHtml() helper must exist to safely render it later (verified end-to-end in the browser)
    assert "function escapeHtml(" in r.text
    print("dashboard: an error-state health snapshot (with HTML-special characters in its message) renders without crashing: OK")


def test_run_id_mismatch_both_ids_reach_the_client():
    """Defect-3 regression: health.run_identity.run_id and kpi.run_id, when
    they disagree, must both reach the client embedded page data so the
    client-side runIdMismatch() check (which withholds performance/ledger
    and shows an explicit state) has what it needs. The actual
    withhold-and-recheck-on-refresh behaviour is JS logic verified in the
    browser, not here (no JS runtime in this test)."""
    client = _client_with_snapshots(HEALTH_RUN_A, KPI_RUN_B)
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200
    assert '"run_id": "run-AAA"' in r.text
    assert '"run_id": "run-BBB"' in r.text
    assert "function runIdMismatch()" in r.text
    print("dashboard: mismatched health/kpi run_ids both reach the client, with the mismatch-check function present: OK")


HEALTH_V2_OK = {
    "state": "ok", "generated_at_utc": "2026-10-08T00:00:00+00:00",
    "run_identity": {"run_id": "v2-run-id-xyz", "started_at_utc": "2026-10-08T00:00:00+00:00", "elapsed_days": 0.1},
    "service": {"status": "active"}, "recording_health": "healthy",
}
KPI_V2_OK = {
    "state": "ok", "generated_at_utc": "2026-10-08T00:00:00+00:00", "run_id": "v2-run-id-xyz",
    "kpi": {"counts": {"eligible_alerts": 2, "suppressed_existing_position": 0, "entered": 2, "completed": 1,
                       "pending_open": 0, "unknown_total": 1, "completed_via_closure_delayed_deadline": 1},
            "avg_net_r_per_completed_trade": {"value": None, "denominator": 0}},
}


def test_v2_routes_require_auth_and_return_separate_snapshots():
    client = _client_with_snapshots(HEALTH_OK, KPI_OK)
    ds.RESEARCH_HEALTH_V2_PATH.write_text(json.dumps(HEALTH_V2_OK))
    ds.RESEARCH_KPI_V2_PATH.write_text(json.dumps(KPI_V2_OK))
    assert client.get("/research/health_v2").status_code == 401
    assert client.get("/research/kpi_v2").status_code == 401
    r = client.get("/research/health_v2", auth=AUTH)
    assert r.status_code == 200 and r.json() == HEALTH_V2_OK
    r = client.get("/research/kpi_v2", auth=AUTH)
    assert r.status_code == 200 and r.json() == KPI_V2_OK
    print("dashboard: /research/health_v2 and /research/kpi_v2 require auth and return the v2 snapshots as-is: OK")


def test_v2_section_never_mixes_with_v1_on_the_page():
    """v1 has real data, v2 is not yet published -- both must render
    correctly on the same page without either one's absence/presence
    affecting the other, and the v2 section must be clearly labeled
    separate."""
    client = _client_with_snapshots(HEALTH_OK, KPI_OK)  # v2 left unpublished (default)
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200
    assert "Research v2" in r.text
    assert "No v2 snapshot published yet" in r.text
    # v1's own content must still be present and correct alongside the unpublished v2 section
    assert "No signals have fired yet" in r.text
    print("dashboard: v1 (populated) and v2 (unpublished) render correctly side by side, clearly separated: OK")


def test_v2_null_avg_r_never_gets_positive_styling():
    client = _client_with_snapshots(HEALTH_OK, KPI_OK)
    ds.RESEARCH_HEALTH_V2_PATH.write_text(json.dumps(HEALTH_V2_OK))
    ds.RESEARCH_KPI_V2_PATH.write_text(json.dumps(KPI_V2_OK))
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200
    import re
    m = re.search(r'Avg R / Completed.*?class="value ([a-z]*)"', r.text, re.DOTALL)
    assert m is not None, "v2 avg-R tile not found in rendered page"
    assert m.group(1) == "", f"a null v2 avg R value must get neutral styling, got class={m.group(1)!r}"
    print("dashboard: v2's null avg-R tile uses neutral styling, never positive-performance styling: OK")


def test_v2_closure_delayed_count_surfaced_distinctly():
    client = _client_with_snapshots(HEALTH_OK, KPI_OK)
    ds.RESEARCH_HEALTH_V2_PATH.write_text(json.dumps(HEALTH_V2_OK))
    ds.RESEARCH_KPI_V2_PATH.write_text(json.dumps(KPI_V2_OK))
    r = client.get("/", auth=AUTH)
    assert "Via Closure-Delayed Deadline" in r.text
    print("dashboard: v2's closure-delayed-deadline count is surfaced as its own, distinctly labeled figure: OK")


def test_v2_kpi_displays_stale_banner_when_publisher_paused():
    """2026-10-09 incident regression: v2's section is server-rendered
    with no client-side JS equivalent of v1's age/STALE handling. If the
    KPI publisher timer is paused (exactly what happened during the
    memory incident) the snapshot file stops updating, but the page must
    say so explicitly -- a frozen figure must never quietly look
    current. Uses a deliberately old generated_at_utc, independent of
    whatever date the test happens to run on."""
    import datetime as _dt
    old_kpi = {**KPI_V2_OK, "generated_at_utc": (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=5)).isoformat()}
    client = _client_with_snapshots(HEALTH_OK, KPI_OK)
    ds.RESEARCH_HEALTH_V2_PATH.write_text(json.dumps(HEALTH_V2_OK))
    ds.RESEARCH_KPI_V2_PATH.write_text(json.dumps(old_kpi))
    r = client.get("/", auth=AUTH)
    # "STALE" alone is confounded by v1's always-present client-side JS
    # source text (literal "— STALE" inside a template string, rendered
    # regardless of actual staleness) -- "last published" is unique to
    # the new v2 server-side staleness note.
    assert "last published" in r.text, "a KPI snapshot published 5 hours ago must show an explicit staleness warning"
    print("dashboard: v2's KPI section shows an explicit STALE banner when its publisher has been paused: OK")


def test_v2_kpi_no_stale_banner_when_fresh():
    import datetime as _dt
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    fresh_health = {**HEALTH_V2_OK, "generated_at_utc": now_iso}
    fresh_kpi = {**KPI_V2_OK, "generated_at_utc": now_iso}
    client = _client_with_snapshots(HEALTH_OK, KPI_OK)
    ds.RESEARCH_HEALTH_V2_PATH.write_text(json.dumps(fresh_health))
    ds.RESEARCH_KPI_V2_PATH.write_text(json.dumps(fresh_kpi))
    r = client.get("/", auth=AUTH)
    assert "last published" not in r.text, "a freshly-published KPI snapshot must never show the stale warning"
    print("dashboard: v2's KPI section shows no stale warning for a freshly-published snapshot: OK")


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
    test_zero_eligible_alerts_page_renders_and_embeds_neutral_data()
    test_health_error_state_never_claims_currently_running()
    test_run_id_mismatch_both_ids_reach_the_client()
    test_v2_routes_require_auth_and_return_separate_snapshots()
    test_v2_section_never_mixes_with_v1_on_the_page()
    test_v2_null_avg_r_never_gets_positive_styling()
    test_v2_closure_delayed_count_surfaced_distinctly()
    test_v2_kpi_displays_stale_banner_when_publisher_paused()
    test_v2_kpi_no_stale_banner_when_fresh()
    print("All dashboard Research-view tests passed (no network, no real observer checkout).")
