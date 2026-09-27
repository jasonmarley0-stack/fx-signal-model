"""Aggregates alert_scorer.py's per-version results into the dashboard's
Performance view — always split by scanner_version, denominators shown
explicitly, never blended (requirement: the H4 config's 3 signals must not
be combined with the legacy M30/28-pair scanner's 1,313 resolved signals).

Also provides a read-only, additive adapter for legacy signals_log /
performance.json data, so it can be listed alongside current data without
ever fabricating a field the old system doesn't have (no alert_id, no
publication-vs-calculation distinction, no bid/ask). Nothing in this file
writes to signals_log, performance.json, or alert_lifecycle_log.jsonl.
"""
from __future__ import annotations
from collections import Counter

# "actionable" = a version whose effective entry window was non-zero, i.e.
# it wasn't cancelled at the same instant it was published.
ACTIONABLE_EXCLUDED_STATES = set()  # every state below already implies a non-zero window; kept explicit for review
STATE_TO_COUNT_KEY = {
    "cancelled": "cancelled",
    "expired_no_entry": "expired",
    "actionable_open": "open",          # never entered yet, window still open
    "open": "open",                     # entered, still within holding period
    "time_exited": "time_exited",
    "stopped": "stopped",
    "targeted": "targeted",
    "no_data": "no_data",
}


def build_alert_performance_view(scored_versions: list[dict]) -> dict:
    """Returns {scanner_version: {issued, actionable, cancelled, expired,
    open, time_exited, stopped, targeted, no_data, win_rate, avg_r}} — one
    block per scanner_version, never merged across versions."""
    by_version: dict[str, list[dict]] = {}
    for r in scored_versions:
        by_version.setdefault(r["scanner_version"], []).append(r)

    out = {}
    for scanner_version, rows in by_version.items():
        counts = Counter()
        r_values = []
        for r in rows:
            counts["issued"] += 1
            if r["state"] != "cancelled" or r.get("assumed_entry_time_utc") is not None:
                counts["actionable"] += 1
            key = STATE_TO_COUNT_KEY.get(r["state"], r["state"])
            counts[key] += 1
            if r.get("r_multiple") is not None:
                r_values.append(r["r_multiple"])
        resolved = counts["stopped"] + counts["targeted"]
        out[scanner_version] = {
            "issued": counts["issued"],
            "actionable": counts["actionable"],
            "cancelled": counts["cancelled"],
            "expired": counts["expired"],
            "open": counts["open"],
            "time_exited": counts["time_exited"],
            "stopped": counts["stopped"],
            "targeted": counts["targeted"],
            "no_data": counts["no_data"],
            "resolved_denominator": resolved,
            "win_rate": (counts["targeted"] / resolved) if resolved else None,
            "avg_r": (sum(r_values) / len(r_values)) if r_values else None,
        }
    return out


LEGACY_SCHEMA = "legacy_v0_no_alert_id"


def legacy_signal_as_lifecycle_view(raw_signal: dict, scored_entry: dict | None) -> dict:
    """Read-only, additive presentation of one OLD signals_log entry (plus
    its performance_scorer.py outcome, if any) in a shape comparable to a
    scored alert version — WITHOUT inventing anything the old system never
    recorded. signals_log/performance.json are not touched by this
    function; it only ever reads them and returns a new dict."""
    scored_entry = scored_entry or {}
    outcome = scored_entry.get("outcome", "not_recorded")
    state_map = {"stop": "stopped", "target": "targeted", "unresolved": "open", "no_data": "no_data"}
    return {
        "alert_id": None,
        "lifecycle_schema": LEGACY_SCHEMA,
        "scanner_version": "not_recorded",  # legacy rows don't carry this; caller should attribute by known cutover dates instead, kept explicit here as unavailable at the row level
        "pair": raw_signal.get("pair"),
        "direction": raw_signal.get("direction"),
        "confidence": raw_signal.get("confidence"),
        "published_at_utc": "not_recorded_assumed_equal_to_logged_at",
        "calculated_at_utc": raw_signal.get("logged_at"),
        "bid_ask_observed": "not_recorded",
        "entry_condition_range": "not_recorded",
        "state": state_map.get(outcome, "not_recorded"),
        "r_multiple": scored_entry.get("r_multiple"),
        "result_type": "estimate",
        "caveats": [
            "Legacy record — no alert_id, no revision/cancellation lineage, no bid/ask, "
            "and no distinction between calculation and publication time was ever recorded.",
            "Read-only view over signals_log/performance.json — nothing was migrated or rewritten.",
        ],
    }
