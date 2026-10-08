"""v2 KPI report. Identical denominator discipline to v1 (see v1's own
report.py) -- the only additions are a contract_version tag and a count of
how many completed trades resolved via the new closure-aware deadline
policy (MEASUREMENT_CONTRACT.md section 4), so that policy's usage is
always visible, never silently blended into an undifferentiated total.
"""
from __future__ import annotations
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from score import build_paper_ledger  # noqa: E402
from run_identity import load_or_create_manifest, CONTRACT_VERSION  # noqa: E402

RESOLVED_STATES = {"stopped", "targeted", "time_exited"}
UNKNOWN_STATES = {"ambiguous_intrabar_exit", "insufficient_data_entry", "incomplete_coverage", "open", "actionable_open"}


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_run(log_dir: Path) -> tuple[list[dict], list[dict], list[dict]]:
    decisions = _read_jsonl(log_dir / "decisions_log.jsonl")
    quotes = _read_jsonl(log_dir / "quotes_log.jsonl")
    health = _read_jsonl(log_dir / "health_log.jsonl")
    return decisions, quotes, health


def kpi_report(log_dir: Path, now: datetime) -> dict:
    load_or_create_manifest(log_dir)
    decisions, quotes, health = load_run(log_dir)
    ledger = build_paper_ledger(decisions, quotes, now)

    executable = [r for r in ledger if r.get("executable")]
    suppressed = [r for r in ledger if not r.get("executable")]

    entered = [r for r in executable if r.get("assumed_entry_time_utc")]
    completed = [r for r in entered if r["state"] in RESOLVED_STATES]
    pending = [r for r in entered if r["state"] == "open"]
    unknown = [r for r in executable if r["state"] in UNKNOWN_STATES]
    missed = [r for r in executable if r["state"] == "expired_no_entry"]
    closure_delayed = [r for r in completed if r.get("deadline_delay_reason") == "market_closure"]
    r_values = [r["r_multiple"] for r in completed if r.get("r_multiple") is not None]

    avg_r_per_completed = (sum(r_values) / len(r_values)) if r_values else None

    all_eligible_denominator = len(executable)
    all_eligible_undetermined = len(unknown) > 0
    all_eligible_numerator = sum(r_values)
    avg_r_per_all_eligible = (all_eligible_numerator / all_eligible_denominator) if (all_eligible_denominator and not all_eligible_undetermined) else None

    by_month = defaultdict(lambda: {"eligible": 0, "entered": 0, "completed": 0, "sum_r": 0.0})
    for r in executable:
        month = r["source_candle_completion_utc"][:7]
        by_month[month]["eligible"] += 1
        if r.get("assumed_entry_time_utc"):
            by_month[month]["entered"] += 1
        if r["state"] in RESOLVED_STATES and r.get("r_multiple") is not None:
            by_month[month]["completed"] += 1
            by_month[month]["sum_r"] += r["r_multiple"]

    resolved_sorted = sorted((r for r in completed if r.get("exit_time_utc")), key=lambda r: r["exit_time_utc"])
    running, peak, max_dd = 0.0, 0.0, 0.0
    curve = []
    for r in resolved_sorted:
        running += r["r_multiple"]
        peak = max(peak, running)
        dd = running - peak
        max_dd = min(max_dd, dd)
        curve.append({"exit_time_utc": r["exit_time_utc"], "pair": r["pair"], "r_multiple": r["r_multiple"],
                      "cumulative_r": running, "drawdown_r": dd, "crossing_type": r.get("crossing_type")})

    decision_delays = [d["decision_delay_seconds"] for d in decisions if d.get("event_type") == "decision" and d.get("decision_delay_seconds") is not None]
    execution_delays = [r["execution_delay_seconds"] for r in completed if r.get("execution_delay_seconds") is not None]
    poll_failures = [h for h in health if h.get("event_type") in ("poll_failed", "quote_tick_crashed", "decision_tick_crashed")]
    quote_attempt_failures = [h for h in health if h.get("event_type") == "quote_attempt" and h.get("outcome") == "failed"]
    quote_attempts_by_category = defaultdict(int)
    for h in quote_attempt_failures:
        quote_attempts_by_category[h.get("error_category", "unknown")] += 1
    no_signal_events = [h for h in health if h.get("event_type") == "no_signal"]

    return {
        "generated_at_utc": now.isoformat(),
        "contract_version": CONTRACT_VERSION,
        "counts": {
            "eligible_alerts": len(executable),
            "suppressed_existing_position": len(suppressed),
            "entered": len(entered),
            "completed": len(completed),
            "pending_open": len(pending),
            "unknown_total": len(unknown),
            "missed_entries_confirmed_zero_pnl": len(missed),
            "completed_via_closure_delayed_deadline": len(closure_delayed),
            "no_signal_ticks": len(no_signal_events),
        },
        "avg_net_r_per_completed_trade": {"value": avg_r_per_completed, "denominator": len(r_values)},
        "avg_net_r_per_all_eligible_alert": {
            "value": avg_r_per_all_eligible,
            "denominator": all_eligible_denominator,
            "is_undetermined": all_eligible_undetermined,
            "reason": f"{len(unknown)} of {all_eligible_denominator} eligible alerts have an unknown outcome" if all_eligible_undetermined else None,
        },
        "max_drawdown_r_partial_completed_trades_only": max_dd,
        "max_drawdown_label": "R drawdown on completed trades only — NOT an account-percentage drawdown; PARTIAL whenever unknown_total > 0",
        "equity_curve": curve,
        "by_month": {m: v for m, v in sorted(by_month.items())},
        "operational": {
            "decision_delay_seconds_mean": (sum(decision_delays) / len(decision_delays)) if decision_delays else None,
            "decision_delay_seconds_max": max(decision_delays) if decision_delays else None,
            "decision_count": len(decision_delays),
            "execution_delay_seconds_mean": (sum(execution_delays) / len(execution_delays)) if execution_delays else None,
            "execution_delay_seconds_max": max(execution_delays) if execution_delays else None,
            "quote_samples_recorded": len(quotes),
            "recording_failures": len(poll_failures),
            "quote_attempt_failures_by_category": dict(quote_attempts_by_category),
            "no_signal_ticks": len(no_signal_events),
        },
        "unobserved": [
            "Financing/swap charges are not recorded or estimated anywhere in this report.",
            "Slippage beyond the sampled bid/ask (i.e. the true fill an order would have received) is not observed — "
            "this reports the quoted price at the sample that crossed a level, not a broker-confirmed fill.",
            "Price movement between quote samples (every QUOTE_SAMPLE_INTERVAL_SECONDS) is unobserved and unobservable "
            "from this data. Every completed outcome is an OBSERVED crossing at the receipt time of the sample that "
            "showed it, not a claim that it was the first instant the level was crossed — see MEASUREMENT_CONTRACT.md.",
        ],
    }


def print_report(report: dict) -> None:
    c = report["counts"]
    print(f"[{report['contract_version']}] Eligible alerts: {c['eligible_alerts']} (suppressed: {c['suppressed_existing_position']})  "
          f"Entered: {c['entered']}  Completed: {c['completed']} ({c['completed_via_closure_delayed_deadline']} via closure-delayed deadline)  "
          f"Pending/open: {c['pending_open']}  Unknown: {c['unknown_total']}  Missed (0 P&L): {c['missed_entries_confirmed_zero_pnl']}")
    avg = report["avg_net_r_per_completed_trade"]
    print(f"Avg net R / completed trade: {avg['value']} (n={avg['denominator']})")
    alleg = report["avg_net_r_per_all_eligible_alert"]
    if alleg["is_undetermined"]:
        print(f"Avg net R / ALL eligible alerts: UNDETERMINED (denominator would be {alleg['denominator']}) — {alleg['reason']}")
    else:
        print(f"Avg net R / ALL eligible alerts: {alleg['value']} (denominator={alleg['denominator']})")
    print(f"Max drawdown (R, partial): {report['max_drawdown_r_partial_completed_trades_only']}")
    op = report["operational"]
    print(f"Quote samples recorded: {op['quote_samples_recorded']}  Recording failures: {op['recording_failures']}  "
          f"Attempt failures by category: {op['quote_attempt_failures_by_category']}")


if __name__ == "__main__":
    from datetime import timezone
    log_dir = Path(__file__).parent / "logs"
    report = kpi_report(log_dir, datetime.now(timezone.utc))
    (Path(__file__).parent / "output").mkdir(exist_ok=True)
    (Path(__file__).parent / "output" / "kpi_report.json").write_text(json.dumps(report, indent=2, default=str))
    print_report(report)
