"""*** RETROSPECTIVE DIAGNOSTIC REPLAY — NOT A FRESH V2 PROSPECTIVE RUN ***

Scores the v1 run's ALREADY-RECORDED decisions (run_id
885c5e3ef4b24446a1be3fc05b8f8577, pinned at 29c286d) under BOTH the frozen
v1 scorer and the new v2 scorer, against the SAME quote data, so the
measurement-contract difference is the only variable. This is a diagnostic
comparison of two ways of measuring the same already-collected evidence --
it is explicitly NOT new prospective observation, NOT a v2 activation, and
its output must never be written into v1's own logs/manifest/KPI output,
and never presented as if it were a fresh v2 run's own result.

Memory-conscious: quotes_log.jsonl is streamed exactly once; this script
operates on an already-bounded extract (see the 7-day diagnosis) rather
than loading the full multi-hundred-MB production log, and reports its own
peak RSS so that's independently checkable, not just asserted.

Usage:
    python3 replay_v1_under_v2.py <decisions.jsonl> <bounded_quotes.jsonl> [--now ISO8601]
"""
from __future__ import annotations
import argparse
import json
import resource
import sys
from datetime import datetime, timezone
from pathlib import Path

V1_FIXTURE = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "pinned_observer_29c286d" / "research" / "prospective_baseline"
V2_DIR = Path(__file__).parent

REPLAY_LABEL = "RETROSPECTIVE_DIAGNOSTIC_REPLAY_V1_DATA_UNDER_V2_CONTRACT"


def _load_v1_score_module():
    import importlib
    import sys as _sys
    sys.path.insert(0, str(V1_FIXTURE))
    for name in ("contract", "score", "report", "run_identity"):
        _sys.modules.pop(name, None)
    v1_contract = importlib.import_module("contract")
    v1_score = importlib.import_module("score")
    return v1_contract, v1_score


def _load_v2_score_module():
    import importlib
    import sys as _sys
    sys.path.insert(0, str(V2_DIR))
    for name in ("contract", "score", "report", "run_identity"):
        _sys.modules.pop(name, None)
    v2_contract = importlib.import_module("contract")
    v2_score = importlib.import_module("score")
    return v2_contract, v2_score


def run_replay(decisions_path: Path, quotes_path: Path, now: datetime) -> dict:
    decisions = [json.loads(l) for l in open(decisions_path) if l.strip()]
    decisions = [d for d in decisions if d.get("event_type") == "decision"]
    quotes = [json.loads(l) for l in open(quotes_path) if l.strip()]  # already a bounded extract -- see module docstring

    v1_contract, v1_score = _load_v1_score_module()
    v2_contract, v2_score = _load_v2_score_module()

    v1_ledger = v1_score.build_paper_ledger(decisions, quotes, now)
    v2_ledger = v2_score.build_paper_ledger(decisions, quotes, now)

    def key(r):
        return (r["pair"], r["actual_recording_time_utc"])

    v1_by_key = {key(r): r for r in v1_ledger}
    v2_by_key = {key(r): r for r in v2_ledger}

    rows = []
    for d in decisions:
        k = (d["pair"], d["actual_recording_time_utc"])
        v1r, v2r = v1_by_key.get(k), v2_by_key.get(k)
        changed = (v1r["executable"], v1r["state"]) != (v2r["executable"], v2r["state"]) if v1r and v2r else None
        reason = None
        if changed:
            if v2r.get("deadline_delay_reason") == "market_closure":
                reason = "v2 executed the deadline after demonstrated market closure (new, labeled exit policy)"
            elif v1r["state"] == "incomplete_coverage" and v2r["state"] in ("stopped", "targeted", "time_exited"):
                reason = "v1 rejected a stale-but-successfully-received provider timestamp that v2 accepts for coverage/pricing"
            elif v1r["state"] != v2r["state"]:
                reason = f"state changed ({v1r['state']} -> {v2r['state']}) for a reason not auto-classified — inspect manually"
        rows.append({
            "pair": d["pair"], "direction": d["direction"], "published_at": d["actual_recording_time_utc"],
            "v1_executable": v1r["executable"] if v1r else None, "v1_state": v1r["state"] if v1r else None,
            "v1_r_multiple": v1r.get("r_multiple") if v1r else None,
            "v2_executable": v2r["executable"] if v2r else None, "v2_state": v2r["state"] if v2r else None,
            "v2_r_multiple": v2r.get("r_multiple") if v2r else None,
            "v2_crossing_type": v2r.get("crossing_type") if v2r else None,
            "v2_deadline_delay_reason": v2r.get("deadline_delay_reason") if v2r else None,
            "classification_changed": changed, "reason": reason,
        })

    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # bytes on macOS, KB on Linux -- reported as-is, labeled
    return {
        "label": REPLAY_LABEL,
        "warning": "This is a retrospective diagnostic replay of ALREADY-RECORDED v1 data under the v2 contract. "
                   "It is NOT a fresh v2 prospective run and must never be presented as one. v1's own logs, "
                   "manifest, and published scores are untouched by this script.",
        "replayed_at_utc": datetime.now(timezone.utc).isoformat(),
        "scoring_clock_now_utc": now.isoformat(),
        "v1_source_run_id": "885c5e3ef4b24446a1be3fc05b8f8577",
        "v1_pinned_commit": "29c286d",
        "decisions_replayed": len(decisions),
        "quote_rows_used": len(quotes),
        "peak_rss": peak_rss_kb,
        "peak_rss_unit": "KB (Linux) or bytes (macOS) per stdlib resource.getrusage — host-dependent, reported as-is",
        "rows": rows,
        "summary": {
            "classifications_changed": sum(1 for r in rows if r["classification_changed"]),
            "v1_completed": sum(1 for r in rows if r["v1_state"] in ("stopped", "targeted", "time_exited")),
            "v2_completed": sum(1 for r in rows if r["v2_state"] in ("stopped", "targeted", "time_exited")),
            "v2_completed_via_closure": sum(1 for r in rows if r["v2_deadline_delay_reason"] == "market_closure"),
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("decisions_path", type=Path)
    parser.add_argument("quotes_path", type=Path)
    parser.add_argument("--now", type=str, default=None)
    args = parser.parse_args()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(timezone.utc)

    result = run_replay(args.decisions_path, args.quotes_path, now)
    out_dir = Path(__file__).parent / "output"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "replay_v1_under_v2_diagnostic.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))

    print(f"*** {result['label']} *** — {result['warning']}")
    print(f"Replayed {result['decisions_replayed']} decisions against {result['quote_rows_used']} quote rows.")
    print(f"Peak RSS: {result['peak_rss']} ({result['peak_rss_unit']})")
    print(f"Classifications changed: {result['summary']['classifications_changed']}")
    print(f"v1 completed: {result['summary']['v1_completed']}  v2 completed: {result['summary']['v2_completed']} "
          f"(of which via closure policy: {result['summary']['v2_completed_via_closure']})")
    print(f"Full detail: {out_path}")
    for r in result["rows"]:
        if r["classification_changed"]:
            print(f"  CHANGED {r['pair']:7s} {r['published_at']}: v1={r['v1_state']} -> v2={r['v2_state']} "
                  f"({r['v2_r_multiple']}) — {r['reason']}")
