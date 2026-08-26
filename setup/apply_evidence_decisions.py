"""Leg 3 of the automated PESTLE evidence review pipeline: reads the
classification decisions the "Signal IQ evidence auto-review" cloud
routine wrote to the review-decisions branch (routines refuse to push
straight to main, so that branch is a dedicated drop-box — see
push_pending_evidence.py's docstring for the full pipeline shape) and
executes them against Signal Engine's real API, running entirely on
localhost the same way setup/process_evidence_batch.py's manual batches
always have: match-company (only for items that weren't already matched)
-> map-signal-type -> approve -> publish, or reject with a reason.

Deliberately never merges review-decisions into main or touches the
working tree's checked-out branch — reads the file straight out of that
ref (git show) so this can't collide with anything else committing to
main around the same time.

Usage:
    python3 setup/apply_evidence_decisions.py

Meant to run under systemd on a recurring timer (see
setup/apply-evidence-decisions.service + .timer), after the routine has
had time to run.
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

import httpx

REPO_DIR = Path(__file__).parent.parent
BASE_URL = "http://127.0.0.1:8000"
ACTOR = "claude-auto-review"  # distinguishes automated decisions from the manual "claude-review-YYYY-MM-DD" batches in the audit trail


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=REPO_DIR, capture_output=True, text=True)


def fetch_decisions() -> list[dict] | None:
    fetch = run("git", "fetch", "origin", "review-decisions")
    if fetch.returncode != 0:
        print("No review-decisions branch to fetch yet (or fetch failed):", fetch.stderr.strip())
        return None
    show = run("git", "show", "origin/review-decisions:review/decisions.json")
    if show.returncode != 0:
        print("review-decisions branch exists but has no review/decisions.json yet:", show.stderr.strip())
        return None
    try:
        return json.loads(show.stdout)
    except json.JSONDecodeError as e:
        print("review/decisions.json on review-decisions branch is not valid JSON:", e)
        return None


def apply_decision(client: httpx.Client, decision: dict) -> str:
    evidence_id = decision["evidence_id"]
    action = decision.get("action")
    notes = decision.get("notes", "")

    if action == "reject":
        r = client.post(f"/raw-evidence/{evidence_id}/reject", json={"actor": ACTOR, "reason": notes or "Rejected by automated review"})
        if r.status_code != 200:
            return f"reject FAILED ({r.status_code}): {r.text[:200]}"
        return "rejected"

    if action != "publish":
        return f"SKIPPED — unrecognized action {action!r}"

    company_id = decision.get("company_id")
    signal_type_id = decision.get("signal_type_id")
    confidence = decision.get("confidence")
    if company_id is None or signal_type_id is None or confidence is None:
        return "publish FAILED — missing company_id/signal_type_id/confidence in decision"

    if not decision.get("was_matched"):
        r = client.post(f"/raw-evidence/{evidence_id}/match-company",
                         json={"actor": ACTOR, "notes": notes, "company_id": company_id})
        if r.status_code != 200:
            return f"match-company FAILED ({r.status_code}): {r.text[:200]}"

    r = client.post(f"/raw-evidence/{evidence_id}/map-signal-type",
                     json={"actor": ACTOR, "notes": notes, "signal_type_id": signal_type_id, "confidence": confidence})
    if r.status_code != 200:
        return f"map-signal-type FAILED ({r.status_code}): {r.text[:200]}"

    r = client.post(f"/raw-evidence/{evidence_id}/approve", json={"actor": ACTOR, "notes": notes})
    if r.status_code != 200:
        return f"approve FAILED ({r.status_code}): {r.text[:200]}"

    r = client.post(f"/raw-evidence/{evidence_id}/publish", json={"actor": ACTOR})
    if r.status_code != 200:
        return f"publish FAILED ({r.status_code}): {r.text[:200]}"
    result = r.json()
    return f"published (signal_id={result.get('published_signal_id')})"


def main():
    decisions = fetch_decisions()
    if not decisions:
        print("Nothing to apply.")
        return

    client = httpx.Client(base_url=BASE_URL, timeout=30)
    published = rejected = failed = 0
    for decision in decisions:
        try:
            outcome = apply_decision(client, decision)
        except Exception as e:  # noqa: BLE001 — one bad decision shouldn't kill the whole batch
            outcome = f"FAILED — unexpected error: {e}"
        print(f"  evidence #{decision.get('evidence_id')}: {outcome}")
        if outcome == "rejected":
            rejected += 1
        elif outcome.startswith("published"):
            published += 1
        else:
            failed += 1

    print(f"\nDone — {published} published, {rejected} rejected, {failed} failed, out of {len(decisions)} decision(s).")


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        print(json.dumps(fetch_decisions(), indent=2))
    else:
        main()
