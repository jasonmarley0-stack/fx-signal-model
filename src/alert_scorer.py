"""Scores each published alert VERSION against the exact levels and timing
published for that version — not a fresh re-evaluation of the pair, and not
the levels of whatever the latest revision happens to be. Reads
alert_lifecycle_log.jsonl (see alert_lifecycle.py); never writes to it.

Every scored result is explicitly labelled an estimate: entry/exit are
scored against OANDA midpoint candles (the only price this system has —
see src/data/oanda.py, price="M" only), "assumed entered" is exactly that,
an assumption for scoring purposes, and notification delay is scored as 0
seconds because no delivery timestamp exists anywhere in this system. None
of this is a realised subscriber return.

Entry/exit discipline (corrected per Codex's review of the first version):
an M30 bar's high/low merely touching the entry range does not establish
an executable fill — only a bar whose OWN OPEN lands inside the range does,
since that is a real, known price at a known time. A range that is only
ever touched, never confirmed by an open, is reported as `insufficient_data`
rather than priced at that bar's close (which can be arbitrarily far
outside the entry condition). Likewise, a bar whose high/low crosses BOTH
the stop and the target cannot have its order determined from OHLC alone
and is reported as `ambiguous_intrabar_exit` rather than assumed
stop-first. A revision or cancellation closes a version's window to NEW
entries only — it does not truncate the post-entry stop/target/time-exit
scan for a subscriber assumed to have already entered under that version
(see IMPLEMENTATION_NOTE_ALERT_LIFECYCLE.md requirement 4).
"""
from __future__ import annotations
from datetime import datetime, timedelta
import pandas as pd

ENTRY_QUOTE_SIDE_ASSUMPTION = "midpoint_no_bidask_available"
NOTIFICATION_DELAY_ASSUMPTION_SECONDS = 0  # not a measurement — no delivery timestamp is recorded anywhere in this system

BASE_CAVEATS = [
    "Scored against OANDA midpoint candles only — no bid/ask observed for this alert.",
    "Entry is an assumption for scoring purposes — Signal IQ has no record of whether any subscriber actually entered.",
    "Notification delay assumed to be 0 seconds — no delivery timestamp is recorded anywhere in this system.",
]


def _parse(t: str) -> datetime:
    return datetime.fromisoformat(t)


def group_lineages(events: list[dict]) -> dict[str, list[dict]]:
    lineages: dict[str, list[dict]] = {}
    for e in events:
        lineages.setdefault(e["lineage_id"], []).append(e)
    for lineage in lineages.values():
        lineage.sort(key=lambda e: e["recorded_at_utc"])
    return lineages


def effective_entry_window_end(version: dict, lineage_sorted: list[dict]) -> tuple[datetime, str]:
    """min(this version's own entry_expiry_utc, the next version's
    publication time if any, a cancellation targeting this exact
    alert_id if any) — see IMPLEMENTATION_NOTE_ALERT_LIFECYCLE.md. This
    only bounds NEW entries; it never bounds the post-entry exit scan for
    a subscriber assumed to have already entered (requirement 4)."""
    own_expiry = _parse(version["entry_expiry_utc"])
    idx = lineage_sorted.index(version)
    candidates = [(own_expiry, "entry_expiry")]
    for later in lineage_sorted[idx + 1:]:
        if later["event_type"] == "cancelled" and later.get("cancelled_alert_id") == version["alert_id"]:
            candidates.append((_parse(later["recorded_at_utc"]), "cancelled"))
            break
        if later["event_type"] in ("issued", "revised"):
            candidates.append((_parse(later["recorded_at_utc"]), "superseded_by_revision"))
            break
    return min(candidates, key=lambda c: c[0])


def _candle_interval(candles: pd.DataFrame) -> timedelta:
    if len(candles.index) >= 2:
        return candles.index[1] - candles.index[0]
    return timedelta(minutes=30)  # this system only ever uses M30 for scoring; a safe fallback for a single-bar fixture


def _find_entry(candles: pd.DataFrame, start: datetime, end: datetime, lo: float, hi: float):
    """window is [start, end) — `end` (entry_expiry / next-version /
    cancellation boundary) is EXCLUSIVE, so a bar starting at or after the
    boundary is never treated as pre-expiry entry activity (the boundary
    bug Codex flagged). Only a bar whose OWN OPEN lands in [lo, hi] confirms
    an entry price; a bar that merely intersects the range without its open
    landing inside it is reported as 'touched', not priced."""
    window = candles[(candles.index >= pd.Timestamp(start)) & (candles.index < pd.Timestamp(end))]
    touched = False
    for ts, bar in window.iterrows():
        if bar["low"] <= hi and bar["high"] >= lo:
            touched = True
            if lo <= bar["open"] <= hi:
                return ts.to_pydatetime(), float(bar["open"]), "confirmed"
    return None, None, ("touched" if touched else "never_reached")


def score_version(version: dict, lineage_sorted: list[dict], candles: pd.DataFrame, now: datetime) -> dict:
    """`candles` must be M30 (or finer) OANDA-shaped OHLC, tz-aware UTC
    index — injected by the caller (live callers fetch it from OANDA; tests
    pass a synthetic DataFrame) so this function has no network dependency
    and is fully unit-testable. Coverage is checked explicitly: this
    function never assumes data it wasn't given (see 'incomplete_coverage'
    below) rather than silently treating missing bars as "nothing happened"."""
    published_at = _parse(version["recorded_at_utc"])
    window_end, window_end_reason = effective_entry_window_end(version, lineage_sorted)

    result = {
        "alert_id": version["alert_id"], "lineage_id": version["lineage_id"],
        "scanner_version": version["scanner_version"], "pair": version["pair"],
        "direction": version["direction"], "confidence": version["confidence"],
        "published_at_utc": version["recorded_at_utc"],
        "effective_entry_window_end_utc": window_end.isoformat(),
        "effective_entry_window_end_reason": window_end_reason,
        "entry_quote_side_assumption": ENTRY_QUOTE_SIDE_ASSUMPTION,
        "notification_delay_assumption_seconds": NOTIFICATION_DELAY_ASSUMPTION_SECONDS,
        "result_type": "estimate",
        "state": None, "assumed_entry_time_utc": None, "assumed_entry_price": None,
        "exit_time_utc": None, "exit_price": None, "r_multiple": None,
        "caveats": list(BASE_CAVEATS),
    }

    if window_end_reason == "cancelled" and window_end <= published_at:
        result["state"] = "cancelled"
        return result

    have_coverage_to_window_end = not candles.empty and candles.index[-1] >= pd.Timestamp(window_end) - _candle_interval(candles)

    entry_time, entry_price, entry_status = _find_entry(
        candles, published_at, window_end, version["entry_condition_lo"], version["entry_condition_hi"])

    if entry_time is None:
        if entry_status == "touched":
            result["state"] = "insufficient_data_entry"
            result["caveats"].append(
                "Price intersected the entry condition range within this window, but no candle's OPEN "
                "confirmed a price inside it — the exact fill point cannot be determined from M30 OHLC "
                "alone, so no entry is assumed rather than inventing one.")
            return result
        if window_end_reason == "cancelled":
            result["state"] = "cancelled"
        elif window_end <= now:
            result["state"] = "expired_no_entry" if have_coverage_to_window_end else "insufficient_data_entry"
            if not have_coverage_to_window_end:
                result["caveats"].append("Candle coverage does not reach this version's entry-window end — cannot confirm expiry without entry.")
        else:
            result["state"] = "actionable_open"
        return result

    result["assumed_entry_time_utc"] = entry_time.isoformat()
    result["assumed_entry_price"] = entry_price

    direction = 1 if version["direction"] == "long" else -1
    stop, target = version["stop"], version["target"]
    risk = abs(entry_price - stop)
    max_exit_time = entry_time + timedelta(hours=version["max_holding_time_hours"])

    # >= entry_time, NOT > — the entry candle itself must be checked too.
    # Entry happens at that candle's OPEN, so the same candle's high/low
    # can still reach stop/target (or both, ambiguously) before the candle
    # closes; excluding it let a later candle's clean target hit override
    # an unresolved or stopped-out entry candle and report a false win
    # (Codex's repro: entry candle crosses both levels, a later candle
    # reaches target, old code reported ~+1R instead of ambiguous).
    post_entry = candles[(candles.index >= pd.Timestamp(entry_time)) & (candles.index <= pd.Timestamp(min(max_exit_time, now)))]
    for ts, bar in post_entry.iterrows():
        hit_stop = (bar["low"] <= stop) if direction == 1 else (bar["high"] >= stop)
        hit_target = (bar["high"] >= target) if direction == 1 else (bar["low"] <= target)
        if hit_stop and hit_target:
            result["state"] = "ambiguous_intrabar_exit"
            result["caveats"].append(
                f"The bar at {ts.isoformat()} crossed both stop and target — M30 OHLC cannot establish "
                f"which was hit first, so no outcome is assumed for this bar rather than guessing.")
            return result
        if hit_stop:
            result.update(state="stopped", exit_time_utc=ts.to_pydatetime().isoformat(), exit_price=stop,
                          r_multiple=(direction * (stop - entry_price) / risk if risk else None))
            return result
        if hit_target:
            result.update(state="targeted", exit_time_utc=ts.to_pydatetime().isoformat(), exit_price=target,
                          r_multiple=(direction * (target - entry_price) / risk if risk else None))
            return result

    if max_exit_time <= now:
        interval = _candle_interval(candles)
        exit_bars = candles[candles.index <= pd.Timestamp(max_exit_time)]
        if not exit_bars.empty and (pd.Timestamp(max_exit_time) - exit_bars.index[-1]) <= interval:
            exit_price = float(exit_bars.iloc[-1]["close"])
            result.update(state="time_exited", exit_time_utc=max_exit_time.isoformat(), exit_price=exit_price,
                          r_multiple=(direction * (exit_price - entry_price) / risk if risk else None))
        else:
            # Coverage doesn't reach max_exit_time — using the nearest older
            # bar would report a stale price as if it were current. Report
            # the gap honestly instead (requirement: "never use an old
            # candle as the price at a later exit").
            result["state"] = "incomplete_coverage"
            result["caveats"].append(
                f"No candle within one bar-interval of the time-exit boundary ({max_exit_time.isoformat()}) "
                f"— cannot price this exit without using a stale bar.")
    else:
        result["state"] = "open"
    return result


def score_all(events: list[dict], candles_by_pair: dict[str, pd.DataFrame], now: datetime, fetch_errors: dict[str, str] | None = None) -> list[dict]:
    """candles_by_pair: {pair: OHLC DataFrame} — one fetch per pair reused
    across every version/lineage for that pair, not one fetch per alert.
    fetch_errors: {pair: error message} for pairs whose candle fetch
    failed — those pairs' versions are scored 'fetch_error' explicitly
    rather than silently omitted or given a plausible-looking result, and
    an error for one pair does not stop any other pair from being scored."""
    fetch_errors = fetch_errors or {}
    out = []
    for lineage in group_lineages(events).values():
        versions = [e for e in lineage if e["event_type"] in ("issued", "revised")]
        for v in versions:
            pair = v["pair"]
            if pair in fetch_errors:
                out.append({"alert_id": v.get("alert_id"), "lineage_id": v.get("lineage_id"),
                            "scanner_version": v.get("scanner_version"), "pair": pair, "direction": v.get("direction"),
                            "state": "fetch_error", "result_type": "estimate",
                            "caveats": [f"OANDA candle fetch failed for {pair}: {fetch_errors[pair]}"]})
                continue
            candles = candles_by_pair.get(pair)
            if candles is None or candles.empty:
                out.append({"alert_id": v.get("alert_id"), "lineage_id": v.get("lineage_id"),
                            "scanner_version": v.get("scanner_version"), "pair": pair, "direction": v.get("direction"),
                            "state": "no_data", "result_type": "estimate",
                            "caveats": ["No OANDA candle data available for this pair/window."]})
                continue
            out.append(score_version(v, lineage, candles, now))
    return out
