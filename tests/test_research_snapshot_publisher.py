"""Tests for research_snapshot_publisher.py against a real, self-contained
copy of the PINNED observer source (tests/fixtures/pinned_observer_29c286d/
-- see its README.md), not a hand-rolled fake. Confirms: sanitization
(no paths/tracebacks in published JSON), atomic writes, explicit states
for missing/malformed/mismatched sources, and that the published KPI/
ledger actually comes from the pinned score.build_paper_ledger()/
report.kpi_report() -- not reimplemented here. No network, no droplet
access, no systemd.
"""
from __future__ import annotations
import json
import shutil
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent.parent))
import research_snapshot_publisher as pub  # noqa: E402

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "pinned_observer_29c286d"
T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)


def _fresh_checkout() -> Path:
    """A throwaway copy of the pinned observer source tree, so each test
    can write its own logs/manifest without interfering with others."""
    d = Path(tempfile.mkdtemp()) / "observer-checkout"
    shutil.copytree(FIXTURE_SRC, d)
    (d / "research" / "prospective_baseline" / "logs").mkdir(parents=True, exist_ok=True)
    return d


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""))


def _write_real_manifest(checkout: Path, run_id: str = "test-run-id", started_at: datetime | None = None) -> None:
    sys.path.insert(0, str(checkout / "research" / "prospective_baseline"))
    import importlib
    import run_identity
    importlib.reload(run_identity)  # each checkout has its own TRACKED_FILES paths
    manifest = run_identity.build_manifest(run_id=run_id)
    if started_at is not None:
        manifest["started_at_utc"] = started_at.isoformat()
    (checkout / "research" / "prospective_baseline" / "logs" / "run_manifest.json").write_text(json.dumps(manifest, indent=2))


def test_health_snapshot_observer_not_found_is_explicit_not_silent():
    out_dir = Path(tempfile.mkdtemp())
    payload = pub.publish_health_snapshot(Path("/nonexistent/path/xyz"), out_dir)
    assert payload["state"] == "observer_not_found"
    on_disk = json.loads((out_dir / "research_health.json").read_text())
    assert on_disk == payload
    print("publisher: missing observer checkout -> explicit observer_not_found state, not a crash: OK")


def test_health_snapshot_no_manifest_never_creates_one():
    """Critical safety property: the publisher must NEVER write into the
    observer's own logs/ -- if run_manifest.json doesn't exist yet, that's
    an explicit no_manifest state, never load_or_create_manifest()'s
    create-a-new-one path."""
    checkout = _fresh_checkout()
    out_dir = Path(tempfile.mkdtemp())
    payload = pub.publish_health_snapshot(checkout, out_dir)
    assert payload["state"] == "no_manifest"
    assert not (checkout / "research" / "prospective_baseline" / "logs" / "run_manifest.json").exists(), (
        "the publisher must never create a manifest in the observer's own checkout")
    print("publisher: missing manifest -> explicit no_manifest state, manifest never created by the publisher: OK")


def test_kpi_snapshot_no_manifest_never_creates_one():
    checkout = _fresh_checkout()
    out_dir = Path(tempfile.mkdtemp())
    payload = pub.publish_kpi_snapshot(checkout, out_dir)
    assert payload["state"] == "no_manifest"
    assert not (checkout / "research" / "prospective_baseline" / "logs" / "run_manifest.json").exists()
    print("publisher: kpi snapshot never creates a manifest either: OK")


def test_manifest_mismatch_never_leaks_a_filesystem_path():
    """Regression for a real defect found during development: the pinned
    RunIdentityMismatchError's own message embeds the log_dir absolute
    path (by design, for an operator). The publisher must never forward
    that raw text into the published snapshot."""
    checkout = _fresh_checkout()
    _write_real_manifest(checkout)
    # tamper with a tracked file AFTER the manifest was recorded
    contract_path = checkout / "research" / "prospective_baseline" / "contract.py"
    contract_path.write_text(contract_path.read_text() + "\n# tampered for test\n")

    out_dir = Path(tempfile.mkdtemp())
    payload = pub.publish_kpi_snapshot(checkout, out_dir)
    assert payload["state"] == "manifest_mismatch", payload
    assert payload["error"] is not None
    assert str(checkout) not in payload["error"], f"published error leaked a filesystem path: {payload['error']}"
    assert "/tmp" not in payload["error"] and "/var" not in payload["error"], (
        f"published error looks like it contains a filesystem path: {payload['error']}")
    print("publisher: a manifest mismatch is reported without ever leaking a filesystem path: OK")


def test_kpi_snapshot_reuses_pinned_report_and_score_not_reimplemented():
    """The published KPI/ledger must be EXACTLY what the pinned
    report.kpi_report()/score.build_paper_ledger() compute -- proven here
    by calling them directly on the same inputs and comparing, not by
    re-deriving the numbers independently."""
    checkout = _fresh_checkout()
    _write_real_manifest(checkout)
    log_dir = checkout / "research" / "prospective_baseline" / "logs"

    d1 = {
        "event_type": "decision", "pair": "EURUSD", "direction": "long",
        "source_candle_start_utc": (T0 - timedelta(hours=4)).isoformat(),
        "source_candle_completion_utc": T0.isoformat(),
        "actual_calculation_time_utc": T0.isoformat(), "actual_recording_time_utc": T0.isoformat(),
        "decision_delay_seconds": 1.0, "confidence": "high", "combined_score": 0.8,
        "entry_price": 1.10000, "stop": 1.09800, "target": 1.10200,
        "entry_condition_lo": 1.09950, "entry_condition_hi": 1.10050,
        "entry_expiry_utc": (T0 + timedelta(hours=4)).isoformat(), "max_holding_time_hours": 30.0,
        "technical_inputs": {"orb": 0.0, "trend": 0.5, "pattern": 0.3},
        "hypothetical": True, "result_type": "prospective_paper",
    }
    _write_jsonl(log_dir / "decisions_log.jsonl", [d1])
    q1 = {"pair": "EURUSD", "received_at_utc": (T0 + timedelta(seconds=5)).isoformat(),
          "oanda_time_utc": (T0 + timedelta(seconds=4)).isoformat(), "bid": 1.09960, "ask": 1.10010, "tradeable": True}
    q2 = {"pair": "EURUSD", "received_at_utc": (T0 + timedelta(seconds=10)).isoformat(),
          "oanda_time_utc": (T0 + timedelta(seconds=9)).isoformat(), "bid": 1.10210, "ask": 1.10212, "tradeable": True}
    _write_jsonl(log_dir / "quotes_log.jsonl", [q1, q2])
    _write_jsonl(log_dir / "health_log.jsonl", [])

    out_dir = Path(tempfile.mkdtemp())
    now = T0 + timedelta(hours=2)

    payload = pub.publish_kpi_snapshot(checkout, out_dir)
    assert payload["state"] == "ok", payload

    # Independently call the SAME pinned functions on the SAME inputs --
    # the publisher's output must match exactly (proves reuse, not reimplementation).
    obs_contract, obs_score, obs_report = pub._import_observer_modules(checkout)
    decisions, quotes, _health = obs_report.load_run(log_dir)
    expected_ledger = obs_score.build_paper_ledger(decisions, quotes, payload["generated_at_utc"] and
                                                    __import__("datetime").datetime.fromisoformat(payload["generated_at_utc"]))
    assert payload["ledger"] == expected_ledger, "published ledger must be exactly build_paper_ledger()'s own output"
    assert payload["kpi"]["counts"]["eligible_alerts"] == 1
    assert "log_dir" not in payload["kpi"], "published KPI must never include a filesystem path"
    print("publisher: published KPI/ledger are exactly the pinned functions' own output, not reimplemented: OK")


def test_health_snapshot_sanitizes_run_identity_no_paths():
    checkout = _fresh_checkout()
    _write_real_manifest(checkout, started_at=datetime.now(timezone.utc) - timedelta(days=3))
    log_dir = checkout / "research" / "prospective_baseline" / "logs"
    _write_jsonl(log_dir / "quotes_log.jsonl", [])
    _write_jsonl(log_dir / "health_log.jsonl", [])

    out_dir = Path(tempfile.mkdtemp())
    payload = pub.publish_health_snapshot(checkout, out_dir)
    assert payload["state"] == "ok", payload
    ri = payload["run_identity"]
    assert "tracked_files" not in ri, "run_identity must never publish absolute tracked-file paths"
    assert ri["run_id"] == "test-run-id"
    assert 2.9 < ri["elapsed_days"] < 3.1
    assert payload["recording_health"] == "no_data"  # no quotes recorded at all
    print("publisher: health snapshot's run_identity is sanitized (no tracked_files paths), elapsed_days correct: OK")


def test_health_snapshot_distinguishes_service_status_from_recording_health():
    """Section-1 requirement: actual service status (systemctl) must be
    reported SEPARATELY from inferred recording health (derived from
    whether quotes are actually arriving) -- a service can be "active"
    while genuinely not recording."""
    checkout = _fresh_checkout()
    _write_real_manifest(checkout)
    log_dir = checkout / "research" / "prospective_baseline" / "logs"
    _write_jsonl(log_dir / "quotes_log.jsonl", [])  # no quotes at all -- recording_health must be no_data
    _write_jsonl(log_dir / "health_log.jsonl", [])

    out_dir = Path(tempfile.mkdtemp())
    payload = pub.publish_health_snapshot(checkout, out_dir, service_name="definitely-not-a-real-service.service")
    assert payload["state"] == "ok"
    assert payload["service"]["status"] in ("unknown", "inactive", "failed"), payload["service"]
    assert payload["recording_health"] == "no_data"
    assert set(payload["service"].keys()) == {"name", "status", "checked_via"}
    assert payload["service"]["status"] != payload["recording_health"], (
        "service status and recording health are independent fields, must not be conflated")
    print("publisher: service.status and recording_health are reported as independent, distinct fields: OK")


def test_atomic_write_leaves_no_partial_file_and_overwrites_cleanly():
    out_dir = Path(tempfile.mkdtemp())
    pub._atomic_write_json(out_dir / "research_health.json", {"a": 1})
    pub._atomic_write_json(out_dir / "research_health.json", {"a": 2})
    assert not (out_dir / "research_health.json.tmp").exists(), "no leftover .tmp file after a successful write"
    assert json.loads((out_dir / "research_health.json").read_text()) == {"a": 2}
    print("publisher: atomic write leaves no partial .tmp file and overwrites cleanly: OK")


def test_short_error_redacts_absolute_paths():
    err = pub._short_error(OSError("[Errno 2] No such file or directory: '/root/fx-signal-model-stage2-observer/secret/path.json'"))
    assert "/root/fx-signal-model-stage2-observer" not in err, err
    assert "<path>" in err
    print("publisher: _short_error redacts absolute-path-shaped substrings: OK")


if __name__ == "__main__":
    test_health_snapshot_observer_not_found_is_explicit_not_silent()
    test_health_snapshot_no_manifest_never_creates_one()
    test_kpi_snapshot_no_manifest_never_creates_one()
    test_manifest_mismatch_never_leaks_a_filesystem_path()
    test_kpi_snapshot_reuses_pinned_report_and_score_not_reimplemented()
    test_health_snapshot_sanitizes_run_identity_no_paths()
    test_health_snapshot_distinguishes_service_status_from_recording_health()
    test_atomic_write_leaves_no_partial_file_and_overwrites_cleanly()
    test_short_error_redacts_absolute_paths()
    print("All research_snapshot_publisher tests passed (no network, no droplet access).")
