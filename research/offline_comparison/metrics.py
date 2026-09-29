"""Aggregation for the replay ledger: per-candidate counts, denominators,
avg net R (eligible and entered), cumulative R / max drawdown, by-month
and by-pair breakdowns, and unknown-outcome bounding. Every function takes
the flat ledger (list of dicts) run_experiment.py produces and a period
filter (dev/holdout), never mixing periods or candidates silently.
"""
from __future__ import annotations
from datetime import datetime
from collections import defaultdict
import pandas as pd

RESOLVED_STATES = {"stopped", "targeted", "time_exited"}
UNKNOWN_STATES = {"ambiguous_intrabar_exit", "insufficient_data_entry", "incomplete_coverage", "open", "actionable_open"}
WIN_STATES = {"targeted"}


def _in_period(row: dict, start: datetime, end: datetime) -> bool:
    t = datetime.fromisoformat(row["recorded_at_utc"])
    return start <= t <= end


def candidate_ledger(ledger: list[dict], candidate: str, start: datetime, end: datetime) -> list[dict]:
    return [r for r in ledger if r.get("candidate") == candidate and _in_period(r, start, end)]


def summarize(rows: list[dict]) -> dict:
    issued_versions = [r for r in rows if r["event_type"] in ("issued", "revised")]
    unique_lineages = {r["lineage_id"] for r in issued_versions if r.get("lineage_id")}
    eligible = [r for r in issued_versions if r["state"] not in ("cancelled",)]  # actionable — had a real window
    entered = [r for r in issued_versions if r.get("assumed_entry_time_utc")]
    resolved = [r for r in entered if r["state"] in RESOLVED_STATES]
    wins = [r for r in resolved if r["state"] in WIN_STATES]
    losses = [r for r in resolved if r["state"] == "stopped"]
    time_exits = [r for r in resolved if r["state"] == "time_exited"]
    profitable_time_exits = [r for r in time_exits if (r.get("r_multiple") or 0) > 0]
    missed = [r for r in issued_versions if r["state"] == "expired_no_entry"]
    cancelled = [r for r in issued_versions if r["state"] == "cancelled"]
    unknown = [r for r in issued_versions if r["state"] in UNKNOWN_STATES]
    suppressed = [r for r in rows if r["event_type"] == "suppressed_existing_position"]

    r_values = [r["r_multiple"] for r in resolved if r.get("r_multiple") is not None]
    avg_r_per_entered = (sum(r_values) / len(r_values)) if r_values else None
    # per-eligible: a confirmed missed entry contributes exactly 0 P&L (it's a
    # real zero, added to the denominator but not the numerator below) —
    # unknown/unresolved cases are excluded from BOTH here, never coerced to
    # 0, and reported separately via unknown_outcome_bounds() instead.
    per_eligible_denominator = len(resolved) + len(missed)
    avg_r_per_eligible = (sum(r_values) / per_eligible_denominator) if per_eligible_denominator else None

    return {
        "issued_versions": len(issued_versions),
        "unique_trade_opportunities": len(unique_lineages),
        "eligible_alerts": len(eligible),
        "assumed_entries": len(entered),
        "completed_trades": len(resolved),
        "wins": len(wins), "losses": len(losses),
        "time_exits": len(time_exits), "profitable_time_exits": len(profitable_time_exits),
        "unprofitable_time_exits": len(time_exits) - len(profitable_time_exits),
        "win_rate": (len(wins) / len(resolved)) if resolved else None,
        "missed_entries": len(missed), "cancelled": len(cancelled),
        "unknown_or_ambiguous": len(unknown),
        "suppressed_existing_position": len(suppressed),
        "avg_net_r_per_entered_trade": avg_r_per_entered,
        "avg_net_r_per_eligible_alert": avg_r_per_eligible,
        "sum_r_resolved": sum(r_values) if r_values else 0.0,
    }


def unknown_outcome_bounds(rows: list[dict]) -> dict:
    """How much the conclusion could move if every currently-unknown
    (ambiguous/insufficient-data/incomplete-coverage/still-open) case
    turned out to be the best- or worst-case r_multiple actually observed
    among this candidate's own resolved trades — a transparent, defensible
    bound, not a guess at what unknown cases "probably" did."""
    issued_versions = [r for r in rows if r["event_type"] in ("issued", "revised")]
    entered = [r for r in issued_versions if r.get("assumed_entry_time_utc")]
    resolved = [r for r in entered if r["state"] in RESOLVED_STATES]
    unknown = [r for r in issued_versions if r["state"] in UNKNOWN_STATES and r.get("assumed_entry_time_utc")]
    r_values = [r["r_multiple"] for r in resolved if r.get("r_multiple") is not None]
    if not r_values or not unknown:
        return {"n_unknown_entered": len(unknown), "best_case_avg_r": None, "worst_case_avg_r": None,
                "observed_avg_r": (sum(r_values) / len(r_values)) if r_values else None}
    best, worst = max(r_values), min(r_values)
    n_unknown = len(unknown)
    observed_sum, observed_n = sum(r_values), len(r_values)
    best_case = (observed_sum + best * n_unknown) / (observed_n + n_unknown)
    worst_case = (observed_sum + worst * n_unknown) / (observed_n + n_unknown)
    return {"n_unknown_entered": n_unknown, "best_case_avg_r": best_case, "worst_case_avg_r": worst_case,
            "observed_avg_r": observed_sum / observed_n}


def cumulative_r_and_drawdown(rows: list[dict]) -> pd.DataFrame:
    resolved = [r for r in rows if r["event_type"] in ("issued", "revised") and r["state"] in RESOLVED_STATES
                and r.get("r_multiple") is not None and r.get("exit_time_utc")]
    resolved.sort(key=lambda r: r["exit_time_utc"])
    if not resolved:
        return pd.DataFrame(columns=["exit_time", "r_multiple", "cumulative_r", "drawdown_r"])
    rows_out, running, peak = [], 0.0, 0.0
    for r in resolved:
        running += r["r_multiple"]
        peak = max(peak, running)
        rows_out.append({"exit_time": r["exit_time_utc"], "pair": r["pair"], "r_multiple": r["r_multiple"],
                          "cumulative_r": running, "drawdown_r": running - peak})
    return pd.DataFrame(rows_out)


def by_month(rows: list[dict], period_start=None, period_end=None) -> pd.DataFrame:
    """One row per calendar month in [period_start, period_end] (default:
    the full span the rows themselves cover), including months with ZERO
    issued alerts — a candidate that stayed silent for a month must show
    a zero row, not simply be absent from the table."""
    issued_versions = [r for r in rows if r["event_type"] in ("issued", "revised")]
    by_m = defaultdict(lambda: {"eligible": 0, "entered": 0, "resolved": 0, "sum_r": 0.0})
    for r in issued_versions:
        month = r["recorded_at_utc"][:7]
        by_m[month]["eligible"] += 0 if r["state"] == "cancelled" else 1
        if r.get("assumed_entry_time_utc"):
            by_m[month]["entered"] += 1
            if r["state"] in RESOLVED_STATES and r.get("r_multiple") is not None:
                by_m[month]["resolved"] += 1
                by_m[month]["sum_r"] += r["r_multiple"]

    if period_start is not None and period_end is not None:
        months = pd.period_range(period_start, period_end, freq="M")
        all_months = [str(p) for p in months]
    else:
        all_months = sorted(by_m.keys())

    return pd.DataFrame([{"month": mo, **by_m.get(mo, {"eligible": 0, "entered": 0, "resolved": 0, "sum_r": 0.0})}
                          for mo in all_months])


def by_pair(rows: list[dict]) -> pd.DataFrame:
    issued_versions = [r for r in rows if r["event_type"] in ("issued", "revised")]
    by_p = defaultdict(lambda: {"eligible": 0, "entered": 0, "resolved": 0, "wins": 0, "sum_r": 0.0})
    for r in issued_versions:
        p = r["pair"]
        by_p[p]["eligible"] += 0 if r["state"] == "cancelled" else 1
        if r.get("assumed_entry_time_utc"):
            by_p[p]["entered"] += 1
            if r["state"] in RESOLVED_STATES and r.get("r_multiple") is not None:
                by_p[p]["resolved"] += 1
                by_p[p]["sum_r"] += r["r_multiple"]
                if r["state"] == "targeted":
                    by_p[p]["wins"] += 1
    return pd.DataFrame([{"pair": p, **v} for p, v in sorted(by_p.items())])
