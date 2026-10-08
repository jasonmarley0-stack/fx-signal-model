"""Run in a SEPARATE subprocess (see test_research_snapshot_publisher.py's
test_v2_publisher_end_to_end_rss_at_production_scale) so the measured peak
RSS reflects only this one end-to-end publish call, not whatever the
parent test process already allocated earlier in its own lifetime --
resource.getrusage's ru_maxrss is a running maximum for the whole process,
so measuring it accurately requires a process of its own.

Builds a real v2 observer checkout fixture with a production-scale
(512,624-row) quotes_log.jsonl and a handful of decisions, calls the REAL
publish_kpi_snapshot() end to end (not just the inner _bounded_quotes()
helper), and prints {"peak_rss_mb": ..., "state": ...} as its only stdout
line so the parent test can read it back.
"""
import json
import platform
import resource
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))
import research_snapshot_publisher as pub  # noqa: E402

FIXTURE_SRC = REPO_ROOT / "tests" / "fixtures" / "pinned_observer_v2_dev"
T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)


def main():
    checkout = Path(tempfile.mkdtemp()) / "observer-checkout-v2"
    shutil.copytree(FIXTURE_SRC, checkout)
    log_dir = checkout / "research" / "prospective_baseline_v2" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(checkout / "research" / "prospective_baseline_v2"))
    import run_identity
    manifest = run_identity.build_manifest(run_id="v2-rss-measurement-run")
    (log_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2))

    decisions = []
    for i, pair in enumerate(["EURUSD", "GBPUSD", "USDJPY"]):
        t = T0 + timedelta(hours=i)
        decisions.append({
            "event_type": "decision", "pair": pair, "direction": "long",
            "source_candle_start_utc": (t - timedelta(hours=4)).isoformat(), "source_candle_completion_utc": t.isoformat(),
            "actual_calculation_time_utc": t.isoformat(), "actual_recording_time_utc": t.isoformat(), "decision_delay_seconds": 1.0,
            "confidence": "high", "combined_score": 0.8, "entry_price": 1.10000, "stop": 1.09800, "target": 1.10200,
            "entry_condition_lo": 1.09950, "entry_condition_hi": 1.10050, "entry_expiry_utc": (t + timedelta(hours=4)).isoformat(),
            "max_holding_time_hours": 30.0, "technical_inputs": {}, "hypothetical": True, "result_type": "prospective_paper_v2",
        })
    (log_dir / "decisions_log.jsonl").write_text("\n".join(json.dumps(d) for d in decisions) + "\n")

    # 512,624: the observed production quote-row count at incident time (see the 7-day diagnosis).
    quotes_path = log_dir / "quotes_log.jsonl"
    pairs_cycle = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
    with quotes_path.open("w") as f:
        for i in range(512_624):
            t = T0 + timedelta(seconds=i * 5)
            pair = pairs_cycle[i % 7]
            f.write(json.dumps({"pair": pair, "received_at_utc": t.isoformat(), "oanda_time_utc": t.isoformat(),
                                "bid": 1.1, "ask": 1.1001, "tradeable": True}) + "\n")

    health_path = log_dir / "health_log.jsonl"
    health_path.write_text("")

    out_dir = Path(tempfile.mkdtemp())
    payload = pub.publish_kpi_snapshot(checkout, out_dir, package_subdir="research/prospective_baseline_v2", snapshot_suffix="_v2")

    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_rss_mb = raw / (1024 * 1024) if platform.system() == "Darwin" else raw / 1024

    print(json.dumps({"peak_rss_mb": peak_rss_mb, "state": payload["state"]}))


if __name__ == "__main__":
    main()
