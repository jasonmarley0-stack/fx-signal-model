"""Offline scorer for the new alert lifecycle — mirrors performance_scorer.py's
shape (reads a log, calls OANDA for the price action that followed, writes
one snapshot JSON) but scores each published alert VERSION (see
alert_lifecycle.py/alert_scorer.py) rather than the latest signal state.

Deliberately kept separate from dashboard_server.py, which never calls
OANDA itself (see its module docstring) — this script is the thing that
would run on its own systemd timer, the same way performance_scorer.py
does, writing alert_performance.json for the dashboard to just read.

NOT wired into systemd by this change (see IMPLEMENTATION_NOTE_ALERT_LIFECYCLE.md,
"do not deploy, restart services, alter production data").

Usage:
    python3 alert_performance_scorer.py
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from data.oanda import fetch_oanda_candles  # noqa: E402
from alert_lifecycle import AlertLifecycleStore  # noqa: E402
from alert_scorer import score_all  # noqa: E402
from alert_performance_view import build_alert_performance_view  # noqa: E402

ALERT_LIFECYCLE_PATH = Path(__file__).parent / "alert_lifecycle_log.jsonl"
ALERT_PERFORMANCE_PATH = Path(__file__).parent / "alert_performance.json"
CANDLE_LOOKBACK_PAD_HOURS = 48  # beyond the longest max_holding_time_hours seen, so a version's whole life fits in one fetch


def fetch_candles_for_events(events: list[dict], now: datetime) -> dict:
    pairs = sorted({e["pair"] for e in events})
    out = {}
    for pair in pairs:
        pair_events = [e for e in events if e["pair"] == pair and e["event_type"] in ("issued", "revised")]
        if not pair_events:
            continue
        earliest = min(datetime.fromisoformat(e["recorded_at_utc"]) for e in pair_events)
        out[pair] = fetch_oanda_candles(
            pair, granularity="M30",
            from_time=earliest,
            to_time=min(now, earliest + timedelta(hours=24 * 60)),  # generous cap; real span is usually far shorter
        )
    return out


def main() -> None:
    store = AlertLifecycleStore(ALERT_LIFECYCLE_PATH)
    events = store.read_all()
    now = datetime.now(timezone.utc)
    print(f"{len(events)} lifecycle event(s) on record.")

    candles_by_pair = fetch_candles_for_events(events, now)
    scored = score_all(events, candles_by_pair, now)
    view = build_alert_performance_view(scored)

    payload = {"updated_at": now.isoformat(), "scored_versions": scored, "by_scanner_version": view}
    tmp = ALERT_PERFORMANCE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(ALERT_PERFORMANCE_PATH)
    print(f"Wrote {ALERT_PERFORMANCE_PATH} — {len(scored)} version(s) scored across {len(view)} scanner_version(s).")


if __name__ == "__main__":
    main()
