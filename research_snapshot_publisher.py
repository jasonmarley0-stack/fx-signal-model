"""Publishes sanitized, pre-computed snapshots of the Stage 2 prospective
observer's state for dashboard_server.py's Research view to read.

This script is the ONLY thing that ever touches the frozen observer
checkout (default /root/fx-signal-model-stage2-observer, pinned at a fixed
commit) or runs its scoring logic. dashboard_server.py's HTTP handlers
never call OANDA, never score anything, never scan raw logs, and never
shell out to systemctl -- they only ever read the two JSON files this
script writes. That split is deliberate: a slow/broken dashboard request
must never affect the observer, and the observer's frozen checkout must
never be touched by dashboard code.

Two independent, separately-schedulable snapshots (own systemd
service+timer pairs -- see setup/research-health-publisher.* and
setup/research-kpi-publisher.*):

  - health  (cheap, frequent -- e.g. every 60s): run identity, actual
    systemctl service status (distinct from INFERRED recording health --
    a service can be "active" while genuinely producing no fresh quotes,
    e.g. over a weekend), per-pair quote freshness/coverage/validity over
    a short recent window, recording-failure counts.
  - kpi     (heavier, less frequent -- e.g. every 15-30 min): calls the
    PINNED report.kpi_report() / score.build_paper_ledger() directly
    (imported from the frozen checkout, never reimplemented here) for the
    full KPI aggregates and the scored trade ledger.

Reads ONLY from the observer checkout's research/prospective_baseline/
logs/ directory (decisions_log.jsonl, quotes_log.jsonl, health_log.jsonl,
run_manifest.json) and its own source files (to import the pinned
functions). Never writes there, never calls load_or_create_manifest() in
a way that could create/modify run_manifest.json (see _read_manifest
below) -- "do not edit its tracked files, change its manifest, restart
it" is enforced by construction, not by convention.

Sanitization: published JSON never contains filesystem paths, credential
values, or raw exception tracebacks. A missing/malformed/stale source, or
any internal error, becomes an explicit `state` field the dashboard can
render distinctly -- never a silently-absent or fabricated figure.

Usage (each mode is its own systemd oneshot, see setup/):
    python3 research_snapshot_publisher.py health [--observer-checkout PATH] [--out-dir PATH]
    python3 research_snapshot_publisher.py kpi    [--observer-checkout PATH] [--out-dir PATH]
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_OBSERVER_CHECKOUT = Path("/root/fx-signal-model-stage2-observer")
DEFAULT_OUT_DIR = Path(__file__).parent / "research_snapshots"
DEFAULT_SERVICE_NAME = "prospective-baseline-observer.service"

# How far back the health snapshot looks for "recent" quote activity --
# independent of, and much shorter than, the KPI snapshot's full-log scan.
HEALTH_WINDOW_SECONDS = 900  # 15 minutes
# A service can be "active" (systemctl) while genuinely not recording --
# this is the threshold past which recording_health downgrades from
# "healthy" even though service.status may still say "active".
RECORDING_STALE_AFTER_SECONDS = 180


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Same convention as live_scan.json/alerts.json/performance.json --
    write to a sibling .tmp file, then replace() (atomic on POSIX), so a
    dashboard request never reads a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, default=str))
    tmp_path.replace(path)


_PATH_RE = re.compile(r"/[\w./-]+")


def _short_error(ex: Exception) -> str:
    """A short, sanitized one-line summary for the published snapshot --
    never the raw traceback. Absolute-path-shaped substrings are redacted
    as a defense-in-depth measure (most exception messages here are
    hand-written and already path-free, but a stdlib OSError/
    JSONDecodeError etc. can embed one). The real traceback still goes to
    stderr/the systemd journal for an operator to read."""
    msg = _PATH_RE.sub("<path>", str(ex))
    return f"{type(ex).__name__}: {msg}"[:300]


def _import_observer_modules(observer_checkout: Path):
    """Imports the PINNED contract/score/report/run_identity modules
    directly from the frozen observer checkout -- this is the "reuse the
    pinned scorer/report functions rather than reimplementing
    calculations" requirement. Nothing here ever writes into
    observer_checkout; only sys.path is touched (process-local, not a
    filesystem change)."""
    obs_pkg = observer_checkout / "research" / "prospective_baseline"
    if not obs_pkg.is_dir():
        raise FileNotFoundError("observer checkout's research/prospective_baseline package not found")
    path_str = str(obs_pkg)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
    import contract as obs_contract  # noqa
    import score as obs_score  # noqa
    import report as obs_report  # noqa
    return obs_contract, obs_score, obs_report


def _log_dir(observer_checkout: Path) -> Path:
    return observer_checkout / "research" / "prospective_baseline" / "logs"


def _read_manifest(log_dir: Path) -> dict | None:
    """Reads run_manifest.json as plain JSON -- deliberately NOT via
    run_identity.load_or_create_manifest(), which would CREATE one (a
    write into the observer's own logs/) if it didn't already exist. The
    observer itself is responsible for creating its manifest; this
    publisher only ever reads it, and returns None (an explicit "no
    manifest yet" state) rather than writing anything if it's missing."""
    manifest_path = log_dir / "run_manifest.json"
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _sanitize_run_identity(manifest: dict) -> dict:
    """Strips tracked_files (absolute local filesystem paths) -- never
    published. Only run_id/started_at_utc/source_hash (a fingerprint, not
    a secret) are exposed."""
    started_at = manifest.get("started_at_utc")
    elapsed_days = None
    if started_at:
        try:
            started = datetime.fromisoformat(started_at)
            elapsed_days = (datetime.now(timezone.utc) - started).total_seconds() / 86400.0
        except ValueError:
            elapsed_days = None
    return {
        "run_id": manifest.get("run_id"),
        "started_at_utc": started_at,
        "source_hash": manifest.get("source_hash"),
        "elapsed_days": elapsed_days,
    }


def _service_status(service_name: str) -> dict:
    """The ONE place systemctl is ever invoked -- inside this standalone
    publisher process, never inside a dashboard HTTP handler. Degrades to
    "unknown" (never raises) if systemctl isn't available at all, e.g.
    when this script is run/tested off the droplet."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True, text=True, timeout=10,
        )
        status = result.stdout.strip() or "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        status = "unknown"
    return {"name": service_name, "status": status, "checked_via": "systemctl"}


def _tail_jsonl(path: Path, max_bytes: int = 512_000) -> list[dict]:
    """Reads only the last `max_bytes` of a (potentially large,
    ever-growing) JSONL log and parses complete lines -- the health
    snapshot only needs a short recent window, not the full history (that
    full-log work belongs to the much less frequent KPI snapshot, which
    calls the pinned report.kpi_report() instead). The first line of the
    tail may be a truncated fragment of an earlier line; it's dropped
    rather than guessed at."""
    if not path.exists():
        return []
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()  # discard the (likely partial) first line
        raw = f.read()
    rows = []
    for line in raw.decode("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def publish_health_snapshot(observer_checkout: Path, out_dir: Path, service_name: str = DEFAULT_SERVICE_NAME) -> dict:
    now = datetime.now(timezone.utc)
    out_path = out_dir / "research_health.json"
    service = _service_status(service_name)

    log_dir = _log_dir(observer_checkout)
    if not log_dir.is_dir():
        payload = {
            "generated_at_utc": now.isoformat(), "state": "observer_not_found", "error": None,
            "run_identity": None, "service": service, "quotes": None,
            "recording_failures": None, "recording_health": "unknown",
        }
        _atomic_write_json(out_path, payload)
        return payload

    manifest = _read_manifest(log_dir)
    if manifest is None:
        payload = {
            "generated_at_utc": now.isoformat(), "state": "no_manifest", "error": None,
            "run_identity": None, "service": service, "quotes": None,
            "recording_failures": None, "recording_health": "unknown",
        }
        _atomic_write_json(out_path, payload)
        return payload

    try:
        obs_contract, obs_score, _obs_report = _import_observer_modules(observer_checkout)
        pairs = obs_contract.PAIRS
        window_start = now - timedelta(seconds=HEALTH_WINDOW_SECONDS)
        recent_quotes = [q for q in _tail_jsonl(log_dir / "quotes_log.jsonl")
                          if (_parse_iso(q.get("received_at_utc")) or now) >= window_start]

        per_pair = {}
        latest_receipt_overall = None
        for pair in pairs:
            pair_quotes = [q for q in recent_quotes if q.get("pair") == pair]
            valid = obs_score._valid_pair_quotes(pair_quotes, pair, now)
            latest = max((_parse_iso(q.get("received_at_utc")) for q in pair_quotes if _parse_iso(q.get("received_at_utc"))), default=None)
            has_gap = obs_score._has_coverage_gap(valid, window_start, now) if valid else True
            age = (now - latest).total_seconds() if latest else None
            per_pair[pair] = {
                "latest_receipt_utc": latest.isoformat() if latest else None,
                "latest_receipt_age_seconds": age,
                "samples_in_window": len(pair_quotes),
                "valid_samples_in_window": len(valid),
                "has_gap": has_gap,
            }
            if latest and (latest_receipt_overall is None or latest > latest_receipt_overall):
                latest_receipt_overall = latest

        pair_coverage_count = sum(1 for p in per_pair.values() if p["latest_receipt_age_seconds"] is not None
                                   and p["latest_receipt_age_seconds"] <= RECORDING_STALE_AFTER_SECONDS)
        total_in_window = len(recent_quotes)
        valid_in_window = sum(p["valid_samples_in_window"] for p in per_pair.values())

        health_events = _tail_jsonl(log_dir / "health_log.jsonl", max_bytes=512_000)
        recording_failures = {
            "poll_failed": sum(1 for h in health_events if h.get("event_type") == "poll_failed"),
            "quote_poll_failed": sum(1 for h in health_events if h.get("event_type") == "quote_poll_failed"),
            "crashed": sum(1 for h in health_events if str(h.get("event_type", "")).endswith("_crashed")),
        }
        recording_failures["total"] = sum(recording_failures.values())

        latest_age_overall = (now - latest_receipt_overall).total_seconds() if latest_receipt_overall else None
        if latest_age_overall is None:
            recording_health = "no_data"
        elif latest_age_overall <= RECORDING_STALE_AFTER_SECONDS and pair_coverage_count == len(pairs):
            recording_health = "healthy"
        elif latest_age_overall <= RECORDING_STALE_AFTER_SECONDS:
            recording_health = "partial"  # some pairs fresh, not all
        else:
            recording_health = "stale"

        payload = {
            "generated_at_utc": now.isoformat(),
            "state": "ok",
            "error": None,
            "run_identity": _sanitize_run_identity(manifest),
            "service": service,
            "quotes": {
                "latest_receipt_utc": latest_receipt_overall.isoformat() if latest_receipt_overall else None,
                "latest_receipt_age_seconds": latest_age_overall,
                "pairs": per_pair,
                "pair_coverage_count": pair_coverage_count,
                "pair_coverage_total": len(pairs),
                "invalid_or_stale_quotes_in_window": total_in_window - valid_in_window,
                "total_quotes_in_window": total_in_window,
                "window_seconds": HEALTH_WINDOW_SECONDS,
            },
            "recording_failures": recording_failures,
            "recording_health": recording_health,
        }
    except Exception as ex:  # noqa: BLE001 -- never let a health tick crash the timer or leak a traceback
        payload = {
            "generated_at_utc": now.isoformat(), "state": "error", "error": _short_error(ex),
            "run_identity": _sanitize_run_identity(manifest), "service": service, "quotes": None,
            "recording_failures": None, "recording_health": "unknown",
        }
        print(f"[research_snapshot_publisher] health snapshot error: {ex}", file=sys.stderr)
        traceback.print_exc()

    _atomic_write_json(out_path, payload)
    return payload


def _parse_iso(t) -> datetime | None:
    if not t:
        return None
    try:
        return datetime.fromisoformat(t)
    except (ValueError, TypeError):
        return None


def publish_kpi_snapshot(observer_checkout: Path, out_dir: Path) -> dict:
    now = datetime.now(timezone.utc)
    out_path = out_dir / "research_kpi.json"

    log_dir = _log_dir(observer_checkout)
    if not log_dir.is_dir():
        payload = {"generated_at_utc": now.isoformat(), "state": "observer_not_found", "error": None,
                   "run_id": None, "kpi": None, "ledger": None}
        _atomic_write_json(out_path, payload)
        return payload

    manifest = _read_manifest(log_dir)
    if manifest is None:
        payload = {"generated_at_utc": now.isoformat(), "state": "no_manifest", "error": None,
                   "run_id": None, "kpi": None, "ledger": None}
        _atomic_write_json(out_path, payload)
        return payload

    try:
        obs_contract, obs_score, obs_report = _import_observer_modules(observer_checkout)
        # The manifest already exists on disk (checked above), so this
        # call can only take report.py's verify-and-compare (read-only)
        # branch, never the create-a-new-manifest (write) branch -- see
        # run_identity.load_or_create_manifest()'s own two branches.
        kpi = obs_report.kpi_report(log_dir, now)
        kpi.pop("log_dir", None)  # never publish a local filesystem path

        decisions, quotes, _health = obs_report.load_run(log_dir)
        ledger = obs_score.build_paper_ledger(decisions, quotes, now)

        payload = {
            "generated_at_utc": now.isoformat(),
            "state": "ok",
            "error": None,
            "run_id": manifest.get("run_id"),
            "kpi": kpi,
            "ledger": ledger,
        }
    except Exception as ex:  # noqa: BLE001
        # run_identity.RunIdentityMismatchError lands here too, along with
        # anything else. Its own message embeds the log_dir filesystem
        # path (by design, for an operator reading the journal) -- so it
        # gets a hand-written safe summary instead of its raw text; every
        # other exception gets a short, generic, path-free summary. Either
        # way: never a raw traceback, and the snapshot's `state` lets the
        # dashboard show an explicit "can't score right now" panel instead
        # of stale or fabricated numbers. The full traceback still goes to
        # stderr/the journal for an operator to read.
        if type(ex).__name__ == "RunIdentityMismatchError":
            state, error = "manifest_mismatch", "Source/contract hash no longer matches this run's recorded manifest."
        else:
            state, error = "error", _short_error(ex)
        payload = {"generated_at_utc": now.isoformat(), "state": state, "error": error,
                   "run_id": manifest.get("run_id"), "kpi": None, "ledger": None}
        print(f"[research_snapshot_publisher] kpi snapshot error: {ex}", file=sys.stderr)
        traceback.print_exc()

    _atomic_write_json(out_path, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["health", "kpi"])
    parser.add_argument("--observer-checkout", type=Path, default=DEFAULT_OBSERVER_CHECKOUT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--service-name", default=DEFAULT_SERVICE_NAME)
    args = parser.parse_args()

    if args.mode == "health":
        payload = publish_health_snapshot(args.observer_checkout, args.out_dir, args.service_name)
    else:
        payload = publish_kpi_snapshot(args.observer_checkout, args.out_dir)

    print(f"Wrote {args.out_dir / ('research_' + args.mode + '.json')} — state={payload['state']}")


if __name__ == "__main__":
    main()
