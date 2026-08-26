"""Leg 1 of the automated PESTLE evidence review pipeline: pulls every
unreviewed raw-evidence item (status NORMALISED or UNMATCHED) from Signal
Engine and pushes it to review/pending_evidence.json on GitHub, so the
"Signal IQ evidence auto-review" cloud routine can read it from its own
checkout — Signal Engine's API is deliberately localhost-only on this
droplet (no auth of its own, not exposed externally), and cloud routine
sandboxes can't reach arbitrary external hosts anyway (confirmed by
testing — see NEXT_STEPS.md), so this relays through GitHub the same way
push_status_snapshot.py already does for the daily health/performance
review.

Pushes straight to main via the same write-scoped deploy key
push_status_snapshot.py uses (the droplet's own git identity, unlike the
routine, has no restriction against committing to main) — this is a swap
of the *previous* pending-evidence snapshot each run, not an accumulating
log, so plain main is fine; only the routine's own write-back
(review/decisions.json, see the "Signal IQ evidence auto-review" routine)
needs the dedicated review-decisions branch, because routines refuse to
push straight to main.

Usage:
    python3 setup/push_pending_evidence.py

Meant to run under systemd on a recurring timer (see
setup/push-pending-evidence.service + .timer), ahead of the routine's own
schedule.
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
from pathlib import Path

import httpx

REPO_DIR = Path(__file__).parent.parent
REVIEW_DIR = REPO_DIR / "review"
PENDING_PATH = REVIEW_DIR / "pending_evidence.json"
BASE_URL = "http://127.0.0.1:8000"


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:600]  # enough for the routine to judge relevance without bloating the file


def fetch_unreviewed() -> list[dict]:
    client = httpx.Client(base_url=BASE_URL, timeout=30)
    offset, out = 0, []
    while True:
        page = client.get("/raw-evidence", params={"limit": 100, "offset": offset}).json()
        for item in page["items"]:
            if item["status"] not in ("NORMALISED", "UNMATCHED"):
                continue
            out.append({
                "id": item["id"],
                "status": item["status"],
                "was_matched": item["status"] == "NORMALISED",  # already has a company_id; leg 3 skips match-company for these
                "company_id": item.get("company_id"),
                "source_id": item["evidence_source_id"],
                "title": item["title"],
                "description": strip_html(item.get("description")),
                "published_at": item["source_published_at"],
                "source_url": item.get("canonical_url"),
            })
        offset += 100
        if offset >= page["total"]:
            break
    return out


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=REPO_DIR, capture_output=True, text=True)


def main():
    items = fetch_unreviewed()
    REVIEW_DIR.mkdir(exist_ok=True)
    PENDING_PATH.write_text(json.dumps(items, indent=2))
    print(f"{len(items)} unreviewed item(s) written to {PENDING_PATH}")

    run("git", "add", "review/pending_evidence.json")
    if run("git", "diff", "--cached", "--quiet").returncode == 0:
        print("No change in pending evidence — skipping commit.")
        return

    commit = run("git", "commit", "-m", f"Automated pending-evidence snapshot ({len(items)} unreviewed)")
    if commit.returncode != 0:
        print("git commit failed:", commit.stderr)
        return

    pull = run("git", "pull", "--rebase", "origin", "main")
    if pull.returncode != 0:
        print("git pull --rebase failed (leaving the local commit in place for next run):", pull.stderr)
        return

    push = run("git", "push", "status-relay", "HEAD:main")
    if push.returncode != 0:
        print("git push failed:", push.stderr)
        return
    print("Pushed pending evidence snapshot.")


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        print(json.dumps(fetch_unreviewed(), indent=2))
    else:
        main()
