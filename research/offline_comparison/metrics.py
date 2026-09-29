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
    """Every averaged metric below states its own denominator explicitly
    in its key name — no bare "avg R" exists, because the correction that
    produced this version found that ambiguity costly: the previous
    "avg_net_r_per_entered_trade" (+0.094 for baseline) was actually
    averaged over completed_trades (130), not assumed_entries (160), and
    "avg_net_r_per_eligible_alert" (+0.086) actually excluded every
    unknown-outcome alert from its denominator (142 known-outcome cases,
    not 244 eligible alerts) despite its name implying full eligible-alert
    coverage. Both are corrected here: the labels now say exactly what
    population each average covers, and a THIRD figure — the true
    all-eligible-alert average — is reported as explicitly undetermined
    when unknown outcomes make it uncomputable, rather than silently
    substituting the known-outcome figure for it."""
    issued_versions = [r for r in rows if r["event_type"] in ("issued", "revised")]
    unique_lineages = {r["lineage_id"] for r in issued_versions if r.get("lineage_id")}
    eligible = [r for r in issued_versions if r["state"] not in ("cancelled",)]  # actionable — had a real window
    entered = [r for r in issued_versions if r.get("assumed_entry_time_utc")]
    resolved = [r for r in entered if r["state"] in RESOLVED_STATES]
    wins = [r for r in resolved if r["state"] in WIN_STATES]  # target-hit rate, its own metric
    losses = [r for r in resolved if r["state"] == "stopped"]
    time_exits = [r for r in resolved if r["state"] == "time_exited"]
    profitable_time_exits = [r for r in time_exits if (r.get("r_multiple") or 0) > 0]
    profitable_trades = [r for r in resolved if (r.get("r_multiple") or 0) > 0]  # target hits + profitable time exits — a SEPARATE metric from target-hit win_rate
    missed = [r for r in issued_versions if r["state"] == "expired_no_entry"]
    cancelled = [r for r in issued_versions if r["state"] == "cancelled"]
    # unknown covers BOTH unknown-entry (insufficient_data_entry — never
    # confirmed a fill) and unknown-exit (ambiguous_intrabar_exit,
    # incomplete_coverage, or genuinely still open) cases.
    unknown = [r for r in issued_versions if r["state"] in UNKNOWN_STATES]
    unknown_entry = [r for r in unknown if r["state"] == "insufficient_data_entry"]
    unknown_exit = [r for r in unknown if r["state"] != "insufficient_data_entry"]
    suppressed = [r for r in rows if r["event_type"] == "suppressed_existing_position"]

    r_values = [r["r_multiple"] for r in resolved if r.get("r_multiple") is not None]
    avg_r_per_completed_trade = (sum(r_values) / len(r_values)) if r_values else None

    # Known-outcome average: resolved trades PLUS confirmed missed entries
    # (which contribute a real, confirmed 0 — not an estimate) — excludes
    # every unknown-entry/unknown-exit case from both numerator and
    # denominator, rather than coercing them to 0 or dropping them silently.
    known_outcome_denominator = len(resolved) + len(missed)
    avg_r_per_known_outcome = (sum(r_values) / known_outcome_denominator) if known_outcome_denominator else None

    # The all-eligible-alert KPI cannot be computed at all while any
    # eligible alert's outcome is unknown — reported as None/undetermined
    # explicitly, never silently equated to the known-outcome average.
    all_eligible_avg_r_undetermined = len(unknown) > 0

    return {
        "issued_versions": len(issued_versions),
        "unique_trade_opportunities": len(unique_lineages),
        "eligible_alerts": len(eligible),
        "assumed_entries": len(entered),
        "completed_trades": len(resolved),
        "wins_target_hit": len(wins), "losses_stopped": len(losses),
        "time_exits": len(time_exits), "profitable_time_exits": len(profitable_time_exits),
        "unprofitable_time_exits": len(time_exits) - len(profitable_time_exits),
        "target_hit_rate_of_completed": (len(wins) / len(resolved)) if resolved else None,
        "profitable_trade_rate_of_completed": (len(profitable_trades) / len(resolved)) if resolved else None,
        "missed_entries_confirmed_zero_pnl": len(missed), "cancelled": len(cancelled),
        "unknown_entry_insufficient_data": len(unknown_entry),
        "unknown_exit_ambiguous_incomplete_or_open": len(unknown_exit),
        "unknown_total": len(unknown),
        "suppressed_existing_position": len(suppressed),
        "avg_net_r_per_completed_trade__denom": len(r_values),
        "avg_net_r_per_completed_trade": avg_r_per_completed_trade,
        "avg_net_r_per_known_outcome__denom": known_outcome_denominator,
        "avg_net_r_per_known_outcome_incl_missed_at_zero": avg_r_per_known_outcome,
        "avg_net_r_per_all_eligible_alert__denom": len(eligible),
        "avg_net_r_per_all_eligible_alert": (avg_r_per_known_outcome if not all_eligible_avg_r_undetermined else None),
        "avg_net_r_per_all_eligible_alert_is_undetermined": all_eligible_avg_r_undetermined,
        "sum_r_completed_trades": sum(r_values) if r_values else 0.0,
    }


def unknown_outcome_sensitivity(rows: list[dict]) -> dict:
    """NOT a guaranteed bound — a labelled sensitivity SCENARIO. Shows
    what the known-outcome average R would become if every currently-
    unknown *entered* case (unknown-exit: ambiguous/incomplete/open) had
    instead matched this candidate's own best- or worst-case OBSERVED
    r_multiple. An unknown case is not mathematically constrained to fall
    within the range of outcomes actually observed — a real unresolved
    trade could in principle do better or worse than anything seen so far
    — so this is stated explicitly as a scenario with that assumption,
    not a bound the true value is guaranteed to sit inside."""
    issued_versions = [r for r in rows if r["event_type"] in ("issued", "revised")]
    entered = [r for r in issued_versions if r.get("assumed_entry_time_utc")]
    resolved = [r for r in entered if r["state"] in RESOLVED_STATES]
    unknown_exit_entered = [r for r in issued_versions if r["state"] in UNKNOWN_STATES
                             and r["state"] != "insufficient_data_entry" and r.get("assumed_entry_time_utc")]
    r_values = [r["r_multiple"] for r in resolved if r.get("r_multiple") is not None]
    if not r_values or not unknown_exit_entered:
        return {"assumption": "if every unknown-exit entered trade matched this candidate's own best/worst OBSERVED outcome — not a guaranteed bound",
                "n_unknown_exit_entered": len(unknown_exit_entered), "scenario_all_best_observed_avg_r": None,
                "scenario_all_worst_observed_avg_r": None,
                "observed_avg_r_completed_trades": (sum(r_values) / len(r_values)) if r_values else None}
    best, worst = max(r_values), min(r_values)
    n_unknown = len(unknown_exit_entered)
    observed_sum, observed_n = sum(r_values), len(r_values)
    scenario_best = (observed_sum + best * n_unknown) / (observed_n + n_unknown)
    scenario_worst = (observed_sum + worst * n_unknown) / (observed_n + n_unknown)
    return {"assumption": "if every unknown-exit entered trade matched this candidate's own best/worst OBSERVED outcome — not a guaranteed bound",
            "n_unknown_exit_entered": n_unknown, "scenario_all_best_observed_avg_r": scenario_best,
            "scenario_all_worst_observed_avg_r": scenario_worst, "observed_avg_r_completed_trades": observed_sum / observed_n}


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
