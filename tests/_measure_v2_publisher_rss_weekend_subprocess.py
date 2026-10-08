"""Run in a SEPARATE subprocess (see test_research_snapshot_publisher.py's
test_v2_publisher_weekend_spanning_rss_at_production_scale), same reasoning
as _measure_v2_publisher_rss_subprocess.py: resource.getrusage's ru_maxrss
is a running maximum for the whole process, so an accurate single-call
measurement needs its own process.

This is the WEEKEND-SPANNING variant required by the closure-integration
correction order: 'remeasure publisher RSS with a weekend-spanning
fixture'. _closure_margin_seconds (research_snapshot_publisher.py) widens
every v2 decision's retained quote window by MAX_CLOSURE_DEADLINE_DELAY_
HOURS so a Friday-deadline/Sunday-execution trade's evidence is never
silently truncated -- this proves that widened window still stays
memory-bounded at production row-count scale (512,624 rows) AND when the
window genuinely contains real closure-shaped data (a tradeable=false
stretch through the weekend plus a reopen), not just extra empty margin.
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

FRIDAY = datetime(2026, 1, 9, 20, 0, 0, tzinfo=timezone.utc)
T0 = FRIDAY - timedelta(hours=30, minutes=-5)  # EURUSD entry: exit deadline lands ~Friday 20:05 UTC
DEADLINE = T0 + timedelta(hours=30)
REOPEN_T = DEADLINE + timedelta(hours=49)  # Sunday reopen


def main():
    checkout = Path(tempfile.mkdtemp()) / "observer-checkout-v2"
    shutil.copytree(FIXTURE_SRC, checkout)
    log_dir = checkout / "research" / "prospective_baseline_v2" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(checkout / "research" / "prospective_baseline_v2"))
    import run_identity
    manifest = run_identity.build_manifest(run_id="v2-weekend-rss-measurement-run")
    (log_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2))

    # Three decisions, as in the non-weekend RSS test, but EURUSD's deadline
    # is deliberately placed so it must be resolved via the closure-delayed
    # exit path -- the one that actually needs the widened window.
    decisions = []
    for pair, published_at in (("EURUSD", T0 - timedelta(minutes=1)), ("GBPUSD", T0 + timedelta(hours=1)), ("USDJPY", T0 + timedelta(hours=2))):
        decisions.append({
            "event_type": "decision", "pair": pair, "direction": "long",
            "source_candle_start_utc": (published_at - timedelta(hours=4)).isoformat(), "source_candle_completion_utc": published_at.isoformat(),
            "actual_calculation_time_utc": published_at.isoformat(), "actual_recording_time_utc": published_at.isoformat(), "decision_delay_seconds": 1.0,
            "confidence": "high", "combined_score": 0.8, "entry_price": 1.10000, "stop": 1.05000, "target": 1.20000,
            "entry_condition_lo": 1.09950, "entry_condition_hi": 1.10050, "entry_expiry_utc": (published_at + timedelta(hours=4)).isoformat(),
            "max_holding_time_hours": 30.0, "technical_inputs": {}, "hypothetical": True, "result_type": "prospective_paper_v2",
        })
    (log_dir / "decisions_log.jsonl").write_text("\n".join(json.dumps(d) for d in decisions) + "\n")

    # 512,624: the observed production quote-row count at incident time (see
    # the 7-day diagnosis), but the stream now genuinely spans the weekend
    # closure window for EURUSD: tradeable=false through Fri-evening to
    # Sunday, then a real tradeable=true reopen, interleaved with ordinary
    # continuous coverage for the other six pairs across the same span.
    quotes_path = log_dir / "quotes_log.jsonl"
    pairs_cycle = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
    start = T0 - timedelta(minutes=5)
    with quotes_path.open("w") as f:
        for i in range(512_624):
            t = start + timedelta(seconds=i * 5)
            pair = pairs_cycle[i % 7]
            if pair == "EURUSD" and DEADLINE <= t <= REOPEN_T:
                tradeable = False if t < REOPEN_T else True
                bid, ask = (1.1002, 1.1003) if not tradeable else (1.10800, 1.10810)
            else:
                tradeable, bid, ask = True, 1.1, 1.1001
            f.write(json.dumps({"pair": pair, "received_at_utc": t.isoformat(), "oanda_time_utc": t.isoformat(),
                                "bid": bid, "ask": ask, "tradeable": tradeable}) + "\n")

    health_path = log_dir / "health_log.jsonl"
    health_path.write_text("")

    out_dir = Path(tempfile.mkdtemp())
    payload = pub.publish_kpi_snapshot(checkout, out_dir, package_subdir="research/prospective_baseline_v2", snapshot_suffix="_v2")

    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_rss_mb = raw / (1024 * 1024) if platform.system() == "Darwin" else raw / 1024

    eurusd_row = next((r for r in (payload.get("ledger") or []) if r.get("pair") == "EURUSD"), None)
    print(json.dumps({"peak_rss_mb": peak_rss_mb, "state": payload["state"],
                       "eurusd_state": eurusd_row.get("state") if eurusd_row else None}))


if __name__ == "__main__":
    main()
