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
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
import pandas as pd

ENTRY_QUOTE_SIDE_ASSUMPTION = "midpoint_no_bidask_available"
NOTIFICATION_DELAY_ASSUMPTION_SECONDS = 0  # not a measurement — no delivery timestamp is recorded anywhere in this system


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
    alert_id if any) — see IMPLEMENTATION_NOTE_ALERT_LIFECYCLE.md."""
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


def _first_bar_in_range(candles: pd.DataFrame, start: datetime, end: datetime, lo: float, hi: float):
    window = candles[(candles.index >= pd.Timestamp(start)) & (candles.index <= pd.Timestamp(end))]
    for ts, bar in window.iterrows():
        if bar["low"] <= hi and bar["high"] >= lo:
            return ts.to_pydatetime(), float(bar["close"])
    return None, None


def score_version(version: dict, lineage_sorted: list[dict], candles: pd.DataFrame, now: datetime) -> dict:
    """`candles` must be M30 (or finer) OANDA-shaped OHLC, tz-aware UTC
    index, spanning from this version's publication through at least
    max_holding_time_hours past its entry_expiry — injected by the caller
    (live callers fetch it from OANDA; tests pass a synthetic DataFrame) so
    this function has no network dependency and is fully unit-testable."""
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
        "caveats": ["Scored against OANDA midpoint candles only — no bid/ask observed for this alert.",
                    "Entry is an assumption for scoring purposes — Signal IQ has no record of whether any subscriber actually entered.",
                    "Notification delay assumed to be 0 seconds — no delivery timestamp is recorded anywhere in this system."],
    }

    if window_end_reason == "cancelled" and window_end <= published_at:
        result["state"] = "cancelled"
        return result

    entry_time, entry_price = _first_bar_in_range(
        candles, published_at, window_end, version["entry_condition_lo"], version["entry_condition_hi"])

    if entry_time is None:
        if window_end_reason == "cancelled":
            result["state"] = "cancelled"
        else:
            result["state"] = "expired_no_entry" if window_end <= now else "actionable_open"
        return result

    result["assumed_entry_time_utc"] = entry_time.isoformat()
    result["assumed_entry_price"] = entry_price

    direction = 1 if version["direction"] == "long" else -1
    stop, target = version["stop"], version["target"]
    risk = abs(entry_price - stop)
    max_exit_time = entry_time + timedelta(hours=version["max_holding_time_hours"])

    post_entry = candles[(candles.index > pd.Timestamp(entry_time)) & (candles.index <= pd.Timestamp(min(max_exit_time, now)))]
    for ts, bar in post_entry.iterrows():
        hit_stop = (bar["low"] <= stop) if direction == 1 else (bar["high"] >= stop)
        hit_target = (bar["high"] >= target) if direction == 1 else (bar["low"] <= target)
        if hit_stop:
            result.update(state="stopped", exit_time_utc=ts.to_pydatetime().isoformat(), exit_price=stop,
                          r_multiple=(direction * (stop - entry_price) / risk if risk else None))
            return result
        if hit_target:
            result.update(state="targeted", exit_time_utc=ts.to_pydatetime().isoformat(), exit_price=target,
                          r_multiple=(direction * (target - entry_price) / risk if risk else None))
            return result

    if max_exit_time <= now:
        exit_bars = candles[candles.index <= pd.Timestamp(max_exit_time)]
        if not exit_bars.empty:
            exit_price = float(exit_bars.iloc[-1]["close"])
            result.update(state="time_exited", exit_time_utc=max_exit_time.isoformat(), exit_price=exit_price,
                          r_multiple=(direction * (exit_price - entry_price) / risk if risk else None))
        else:
            result.update(state="time_exited", exit_time_utc=max_exit_time.isoformat(), exit_price=None, r_multiple=None)
    else:
        result["state"] = "open"
    return result


def score_all(events: list[dict], candles_by_pair: dict[str, pd.DataFrame], now: datetime) -> list[dict]:
    """candles_by_pair: {pair: OHLC DataFrame} — one fetch per pair reused
    across every version/lineage for that pair, not one fetch per alert."""
    out = []
    for lineage in group_lineages(events).values():
        versions = [e for e in lineage if e["event_type"] in ("issued", "revised")]
        for v in versions:
            candles = candles_by_pair.get(v["pair"])
            if candles is None or candles.empty:
                out.append({**{k: v.get(k) for k in ("alert_id", "lineage_id", "scanner_version", "pair", "direction")},
                            "state": "no_data", "result_type": "estimate",
                            "caveats": ["No OANDA candle data available for this pair/window."]})
                continue
            out.append(score_version(v, lineage, candles, now))
    return out
