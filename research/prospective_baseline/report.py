"""KPI report for the prospective baseline observation run. Reads
decisions_log.jsonl + quotes_log.jsonl + health_log.jsonl from a log
directory, scores every decision (score.py), and reports exactly the
metrics requested — same denominator discipline as
research/offline_comparison/metrics.py (every average states what it
covers; an all-eligible-alert average is reported undetermined, never
substituted, when any outcome is unknown).
"""
from __future__ import annotations
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from score import score_all  # noqa: E402

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
    decisions, quotes, health = load_run(log_dir)
    scored = score_all(decisions, quotes, now)

    entered = [r for r in scored if r.get("assumed_entry_time_utc")]
    completed = [r for r in entered if r["state"] in RESOLVED_STATES]
    pending = [r for r in entered if r["state"] == "open"]
    unknown = [r for r in scored if r["state"] in UNKNOWN_STATES]
    r_values = [r["r_multiple"] for r in completed if r.get("r_multiple") is not None]

    avg_r_per_completed = (sum(r_values) / len(r_values)) if r_values else None
    all_eligible_undetermined = len(unknown) > 0

    by_month = defaultdict(lambda: {"eligible": 0, "entered": 0, "completed": 0, "sum_r": 0.0})
    for r in scored:
        month = r["source_candle_completion_utc"][:7]
        by_month[month]["eligible"] += 1
        if r.get("assumed_entry_time_utc"):
            by_month[month]["entered"] += 1
        if r["state"] in RESOLVED_STATES and r.get("r_multiple") is not None:
            by_month[month]["completed"] += 1
            by_month[month]["sum_r"] += r["r_multiple"]

    # cumulative R / partial drawdown, completed trades only, chronological by exit
    resolved_sorted = sorted((r for r in completed if r.get("exit_time_utc")), key=lambda r: r["exit_time_utc"])
    running, peak, max_dd = 0.0, 0.0, 0.0
    curve = []
    for r in resolved_sorted:
        running += r["r_multiple"]
        peak = max(peak, running)
        dd = running - peak
        max_dd = min(max_dd, dd)
        curve.append({"exit_time_utc": r["exit_time_utc"], "pair": r["pair"], "r_multiple": r["r_multiple"],
                      "cumulative_r": running, "drawdown_r": dd})

    decision_delays = [d["decision_delay_seconds"] for d in decisions if d.get("event_type") == "decision" and d.get("decision_delay_seconds") is not None]
    poll_failures = [h for h in health if h.get("event_type") in ("poll_failed", "quote_poll_failed", "decision_tick_crashed", "quote_tick_crashed")]
    no_signal_events = [h for h in health if h.get("event_type") == "no_signal"]

    return {
        "generated_at_utc": now.isoformat(),
        "log_dir": str(log_dir),
        "counts": {
            "eligible_alerts": len(scored),
            "entered": len(entered),
            "completed": len(completed),
            "pending_open": len(pending),
            "unknown_total": len(unknown),
            "no_signal_ticks": len(no_signal_events),
        },
        "avg_net_r_per_completed_trade": {"value": avg_r_per_completed, "denominator": len(r_values)},
        "avg_net_r_per_all_eligible_alert": {
            "value": None if all_eligible_undetermined else avg_r_per_completed,
            "is_undetermined": all_eligible_undetermined,
            "reason": f"{len(unknown)} of {len(scored)} eligible alerts have an unknown outcome" if all_eligible_undetermined else None,
        },
        "max_drawdown_r_partial_completed_trades_only": max_dd,
        "max_drawdown_label": "R drawdown on completed trades only — NOT an account-percentage drawdown; PARTIAL whenever unknown_total > 0",
        "equity_curve": curve,
        "by_month": {m: v for m, v in sorted(by_month.items())},
        "operational": {
            "decision_delay_seconds_mean": (sum(decision_delays) / len(decision_delays)) if decision_delays else None,
            "decision_delay_seconds_max": max(decision_delays) if decision_delays else None,
            "decision_count": len(decision_delays),
            "quote_samples_recorded": len(quotes),
            "recording_failures": len(poll_failures),
            "no_signal_ticks": len(no_signal_events),
        },
        "unobserved": [
            "Financing/swap charges are not recorded or estimated anywhere in this report.",
            "Slippage beyond the sampled bid/ask (i.e. the true fill an order would have received) is not observed — "
            "this reports the quoted price at the sample that crossed a level, not a broker-confirmed fill.",
            "Price movement between quote samples (every QUOTE_SAMPLE_INTERVAL_SECONDS) is unobserved and unobservable "
            "from this data — a real coverage limit distinct from the offline replay's continuous M30-candle coverage.",
        ],
    }


def print_report(report: dict) -> None:
    c = report["counts"]
    print(f"Eligible alerts: {c['eligible_alerts']}  Entered: {c['entered']}  Completed: {c['completed']}  "
          f"Pending/open: {c['pending_open']}  Unknown: {c['unknown_total']}")
    avg = report["avg_net_r_per_completed_trade"]
    print(f"Avg net R / completed trade: {avg['value']} (n={avg['denominator']})")
    alleg = report["avg_net_r_per_all_eligible_alert"]
    print(f"Avg net R / ALL eligible alerts: {'UNDETERMINED — ' + alleg['reason'] if alleg['is_undetermined'] else alleg['value']}")
    print(f"Max drawdown (R, partial, completed trades only): {report['max_drawdown_r_partial_completed_trades_only']}")
    print("By month:")
    for m, v in report["by_month"].items():
        print(f"  {m}: eligible={v['eligible']} entered={v['entered']} completed={v['completed']} sum_r={v['sum_r']:.3f}")
    op = report["operational"]
    print(f"Decision delay (s): mean={op['decision_delay_seconds_mean']} max={op['decision_delay_seconds_max']} n={op['decision_count']}")
    print(f"Quote samples recorded: {op['quote_samples_recorded']}  Recording failures: {op['recording_failures']}  No-signal ticks: {op['no_signal_ticks']}")
    print("Unobserved:")
    for u in report["unobserved"]:
        print(f"  - {u}")


if __name__ == "__main__":
    from datetime import timezone
    log_dir = Path(__file__).parent / "logs"
    report = kpi_report(log_dir, datetime.now(timezone.utc))
    (Path(__file__).parent / "output").mkdir(exist_ok=True)
    (Path(__file__).parent / "output" / "kpi_report.json").write_text(json.dumps(report, indent=2, default=str))
    print_report(report)
