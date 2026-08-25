"""Pushes a small daily status snapshot to this repo's status/ directory, so
the scheduled cloud monitoring routine (see /schedule) can read it from its
own git checkout instead of reaching the droplet over the network directly.

Why this exists: cloud routine sandboxes sit behind a strict outbound
allowlist that does not include arbitrary third-party hosts — confirmed
2026-08-25 by testing directly: a plain HTTPS call from a routine to the
droplet's own dashboard (even behind Caddy with a real cert) got a 403 from
the sandbox's own outbound proxy gateway, before the request ever left the
sandbox. GitHub, by contrast, is already a routine's normal, sanctioned
access path (that's how `sources: [{"git_repository": ...}]` works) — so
this relays the same data through there instead.

Reads only the same local snapshot files dashboard_server.py itself reads
(never calls OANDA or Signal Engine directly) plus systemd service/timer
status, and commits+pushes the result via a dedicated write-scoped deploy
key (~/.ssh/fx_signal_model_status_relay, wired to the `status-relay` git
remote via ~/.ssh/config) — kept separate from both the read-only deploy
key used for the private signal-engine repo and this Mac's own push access.
A no-op (no commit) if nothing changed since the last run.

Usage:
    python3 setup/push_status_snapshot.py

Meant to run under systemd on a daily timer (see
setup/push-status-snapshot.service + .timer), scheduled to land before the
monitoring routine's own daily run.
"""
from __future__ import annotations
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent
STATUS_DIR = REPO_DIR / "status"
STATUS_PATH = STATUS_DIR / "daily_status.json"

SERVICES = ["signal-scanner", "dashboard-server", "signal-engine"]
TIMERS = ["rss-ingestion.timer", "performance-scorer.timer"]


def systemctl(*args: str) -> str:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True).stdout.strip()


def service_status(name: str) -> dict:
    return {"active": systemctl("is-active", name), "enabled": systemctl("is-enabled", name)}


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=REPO_DIR, capture_output=True, text=True)


def main():
    now = datetime.now(timezone.utc)

    live_scan = load_json(REPO_DIR / "live_scan.json")
    live_scan_age_seconds = None
    if live_scan:
        generated = datetime.fromisoformat(live_scan["generated_at"])
        live_scan_age_seconds = (now - generated).total_seconds()

    performance = load_json(REPO_DIR / "performance.json") or {}
    alerts = (load_json(REPO_DIR / "alerts.json") or {}).get("alerts", [])

    snapshot = {
        "generated_at": now.isoformat(),
        "services": {name: service_status(name) for name in SERVICES},
        "timers": {name: systemctl("is-active", name) for name in TIMERS},
        "live_scan_age_seconds": live_scan_age_seconds,
        "performance_updated_at": performance.get("updated_at"),
        "performance_aggregates": performance.get("aggregates"),
        "alerts_stored_count": len(alerts),
        "most_recent_alert": alerts[-1] if alerts else None,
    }

    STATUS_DIR.mkdir(exist_ok=True)
    STATUS_PATH.write_text(json.dumps(snapshot, indent=2))

    run("git", "add", "status/daily_status.json")
    if run("git", "diff", "--cached", "--quiet").returncode == 0:
        print("No change in status snapshot — skipping commit.")
        return

    commit = run("git", "commit", "-m", f"Automated status snapshot {now.strftime('%Y-%m-%d %H:%M UTC')}")
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
    print("Pushed status snapshot.")


if __name__ == "__main__":
    main()
