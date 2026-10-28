"""Manual subprocess benchmark: 10 vs 50 dense, non-overlapping SAME-pair trades.

Exercises publish_kpi_snapshot end-to-end plus a growing health log, rather
than a synthetic large raw log with almost no retained relevant data.
"""
import json
import platform
import resource
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import research_snapshot_publisher as pub


def main(count, window_hours=30):
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        checkout = base / "observer"
        shutil.copytree(ROOT / "tests/fixtures/pinned_observer_v2_dev", checkout)
        logs = checkout / "research/prospective_baseline_v2/logs"
        logs.mkdir()
        _, _, report = pub._import_observer_modules(checkout, "research/prospective_baseline_v2")
        report.load_or_create_manifest(logs)  # synthetic fixture only
        with (logs / "decisions_log.jsonl").open("w") as decs, (logs / "quotes_log.jsonl").open("w") as quotes, (logs / "health_log.jsonl").open("w") as health:
            for i in range(count):
                t = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=i * 5)
                d = dict(event_type="decision", pair="EURUSD", direction="long", entry_price=1.1001,
                         stop=1.09, target=1.12, entry_condition_lo=1.1, entry_condition_hi=1.101,
                         source_candle_completion_utc=t.isoformat(), actual_recording_time_utc=t.isoformat(),
                         entry_expiry_utc=(t + timedelta(hours=4)).isoformat(), max_holding_time_hours=30,
                         decision_delay_seconds=0)
                decs.write(json.dumps(d) + "\n")
                # Every 5 seconds for the FULL 30h, including actual deadline.
                for s in range(0, window_hours * 3600 + 1, 5):
                    stamp = (t + timedelta(seconds=s)).isoformat()
                    quotes.write(json.dumps(dict(pair="EURUSD", received_at_utc=stamp, oanda_time_utc=stamp,
                                                 bid=1.1, ask=1.1001, tradeable=True)) + "\n")
                    health.write(json.dumps(dict(event_type="quote_attempt", outcome="success", time_utc=stamp)) + "\n")
        before = time.monotonic()
        result = pub.publish_kpi_snapshot(checkout, base / "snapshots", "research/prospective_baseline_v2", "_v2")
        elapsed = time.monotonic() - before
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 ** 2 if platform.system() == "Darwin" else 1024)
        assert result["state"] == "ok", result
        assert result["kpi"]["counts"]["completed"] == count
        rows = count * (window_hours * 720 + 1)
        assert result["kpi"]["operational"]["quote_samples_recorded"] == rows
        print(json.dumps(dict(decisions=count, window_hours=window_hours, quote_rows=rows, health_rows=rows,
                              completed=result["kpi"]["counts"]["completed"], peak_rss_mib=round(rss, 2),
                              publication_seconds=round(elapsed, 2))))


if __name__ == "__main__":
    main(int(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 30)
