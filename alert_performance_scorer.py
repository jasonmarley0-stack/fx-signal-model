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
COVERAGE_PAD_HOURS = 3  # beyond each version's own max_holding_time_hours, so its time-exit bar is actually covered
MAX_SINGLE_FETCH_DAYS = 60  # a conservative per-request span; longer windows are chunked, not requested in one call


def bounded_fetch(pair: str, start: datetime, end: datetime):
    """Chunks [start, end) into <= MAX_SINGLE_FETCH_DAYS windows — "bounded/
    paginated windows appropriate to OANDA's API" rather than one
    arbitrarily long request. Concatenates and dedupes overlapping bars at
    chunk boundaries."""
    import pandas as pd
    frames = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=MAX_SINGLE_FETCH_DAYS), end)
        frames.append(fetch_oanda_candles(pair, granularity="M30", from_time=cursor, to_time=chunk_end))
        cursor = chunk_end
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    combined = pd.concat(frames)
    return combined[~combined.index.duplicated(keep="first")].sort_index()


def fetch_candles_for_events(events: list[dict], now: datetime) -> tuple[dict, dict]:
    """Per-PAIR window sized from every one of that pair's own versions'
    own need (publish time through its own max_holding_time_hours past
    entry, plus a coverage pad) — not capped relative to the pair's
    FIRST-EVER alert, so a version fired well over 60 days after that
    first alert is still fully covered as history grows. Returns
    (candles_by_pair, fetch_errors) — a fetch failure for one pair is
    recorded and that pair's versions are scored 'fetch_error' by
    score_all(), not silently blank, and does not stop any other pair."""
    by_pair_versions: dict[str, list[dict]] = {}
    for e in events:
        if e["event_type"] in ("issued", "revised"):
            by_pair_versions.setdefault(e["pair"], []).append(e)

    candles_by_pair, fetch_errors = {}, {}
    for pair, versions in by_pair_versions.items():
        try:
            earliest = min(datetime.fromisoformat(v["recorded_at_utc"]) for v in versions)
            latest_need = max(
                datetime.fromisoformat(v["recorded_at_utc"]) + timedelta(hours=v["max_holding_time_hours"] + COVERAGE_PAD_HOURS)
                for v in versions
            )
            candles_by_pair[pair] = bounded_fetch(pair, earliest, min(now, latest_need))
        except Exception as ex:  # noqa: BLE001 — one pair's fetch failure must not stop the others
            fetch_errors[pair] = str(ex)
            print(f"  {pair}: OANDA fetch error (non-fatal, other pairs unaffected) — {ex}")
    return candles_by_pair, fetch_errors


def main() -> None:
    store = AlertLifecycleStore(ALERT_LIFECYCLE_PATH)
    events = store.read_all()
    now = datetime.now(timezone.utc)
    print(f"{len(events)} lifecycle event(s) on record.")

    candles_by_pair, fetch_errors = fetch_candles_for_events(events, now)
    scored = score_all(events, candles_by_pair, now, fetch_errors=fetch_errors)
    view = build_alert_performance_view(scored)

    payload = {"updated_at": now.isoformat(), "scored_versions": scored, "by_scanner_version": view}
    tmp = ALERT_PERFORMANCE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(ALERT_PERFORMANCE_PATH)
    print(f"Wrote {ALERT_PERFORMANCE_PATH} — {len(scored)} version(s) scored across {len(view)} scanner_version(s).")


if __name__ == "__main__":
    main()
