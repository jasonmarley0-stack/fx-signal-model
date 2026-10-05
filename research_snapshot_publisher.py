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
import importlib
import json
import re
import subprocess
import sys
import traceback
from collections import defaultdict
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
RECENT_LEDGER_LIMIT = 100


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
    # Publisher tests legitimately use more than one throwaway frozen
    # checkout in a process.  Never let a prior checkout's generic module
    # names (contract/score/report/run_identity) leak into the next one.
    for name in ("contract", "score", "report", "run_identity"):
        sys.modules.pop(name, None)
    obs_contract = importlib.import_module("contract")
    obs_score = importlib.import_module("score")
    obs_report = importlib.import_module("report")
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


def _iter_jsonl(path: Path):
    """Stream JSONL one row at a time: KPI publication must not retain the
    observer's ever-growing quote log in memory."""
    if not path.exists():
        return
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _decision_windows(decisions: list[dict], quote_staleness_seconds: float) -> dict[str, tuple[datetime, datetime]]:
    """Smallest conservative per-pair quote windows needed by the frozen
    scorer.  A decision can enter just before its deadline and then require
    its full holding period plus one permitted post-deadline sample."""
    windows = {}
    for d in decisions:
        if d.get("event_type") != "decision":
            continue
        start = _parse_iso(d.get("actual_recording_time_utc"))
        deadline = _parse_iso(d.get("entry_expiry_utc"))
        if start is None or deadline is None:
            continue
        end = deadline + timedelta(hours=float(d["max_holding_time_hours"]), seconds=quote_staleness_seconds)
        pair = d["pair"]
        if pair in windows:
            old_start, old_end = windows[pair]
            windows[pair] = min(old_start, start), max(old_end, end)
        else:
            windows[pair] = start, end
    return windows


def _bounded_quotes(path: Path, windows: dict[str, tuple[datetime, datetime]]) -> tuple[dict[str, list[dict]], int]:
    """One streaming pass over the raw quote log.  Only quotes which can
    affect at least one frozen decision score are retained."""
    out = defaultdict(list)
    scanned = 0
    for q in _iter_jsonl(path):
        scanned += 1
        pair = q.get("pair")
        window = windows.get(pair)
        if window is None:
            continue
        received = _parse_iso(q.get("received_at_utc"))
        if received is not None and window[0] <= received <= window[1]:
            out[pair].append(q)
    return out, scanned


def _bounded_ledger(obs_score, decisions: list[dict], quote_path: Path, now: datetime, quote_staleness_seconds: float) -> tuple[list[dict], int, int]:
    windows = _decision_windows(decisions, quote_staleness_seconds)
    quotes_by_pair, scanned = _bounded_quotes(quote_path, windows)
    ledger = []
    # Position suppression is pair-local in the frozen build_paper_ledger;
    # invoking it once per pair is therefore semantically identical while
    # keeping only that pair's decision-relevant quote window in memory.
    by_pair = defaultdict(list)
    for d in decisions:
        if d.get("event_type") == "decision":
            by_pair[d["pair"]].append(d)
    retained = 0
    for pair, pair_decisions in by_pair.items():
        pair_quotes = quotes_by_pair.get(pair, [])
        retained += len(pair_quotes)
        ledger.extend(obs_score.build_paper_ledger(pair_decisions, pair_quotes, now))
    ledger.sort(key=lambda r: r["actual_recording_time_utc"])
    return ledger, scanned, retained


def _kpi_from_frozen_ledger(ledger: list[dict], decisions: list[dict], health: list[dict], now: datetime) -> dict:
    """Aggregate the frozen ledger with report.py's unchanged denominator
    definitions.  Scoring/state decisions remain exclusively frozen code."""
    resolved = {"stopped", "targeted", "time_exited"}
    unknown_states = {"ambiguous_intrabar_exit", "insufficient_data_entry", "incomplete_coverage", "open", "actionable_open"}
    executable = [r for r in ledger if r.get("executable")]
    suppressed = [r for r in ledger if not r.get("executable")]
    entered = [r for r in executable if r.get("assumed_entry_time_utc")]
    completed = [r for r in entered if r["state"] in resolved]
    pending = [r for r in entered if r["state"] == "open"]
    unknown = [r for r in executable if r["state"] in unknown_states]
    missed = [r for r in executable if r["state"] == "expired_no_entry"]
    r_values = [r["r_multiple"] for r in completed if r.get("r_multiple") is not None]
    by_month = defaultdict(lambda: {"eligible": 0, "entered": 0, "completed": 0, "sum_r": 0.0})
    for r in executable:
        b = by_month[r["source_candle_completion_utc"][:7]]; b["eligible"] += 1
        if r.get("assumed_entry_time_utc"): b["entered"] += 1
        if r["state"] in resolved and r.get("r_multiple") is not None:
            b["completed"] += 1; b["sum_r"] += r["r_multiple"]
    running = peak = max_dd = 0.0; curve = []
    for r in sorted((x for x in completed if x.get("exit_time_utc")), key=lambda x: x["exit_time_utc"]):
        running += r["r_multiple"]; peak = max(peak, running); dd = running - peak; max_dd = min(max_dd, dd)
        curve.append({"exit_time_utc": r["exit_time_utc"], "pair": r["pair"], "r_multiple": r["r_multiple"], "cumulative_r": running, "drawdown_r": dd})
    decision_delays = [d["decision_delay_seconds"] for d in decisions if d.get("event_type") == "decision" and d.get("decision_delay_seconds") is not None]
    execution_delays = [r["execution_delay_seconds"] for r in completed if r.get("execution_delay_seconds") is not None]
    failures = [h for h in health if h.get("event_type") in ("poll_failed", "quote_poll_failed", "decision_tick_crashed", "quote_tick_crashed")]
    no_signal = [h for h in health if h.get("event_type") == "no_signal"]
    return {"generated_at_utc": now.isoformat(), "counts": {"eligible_alerts": len(executable), "suppressed_existing_position": len(suppressed), "entered": len(entered), "completed": len(completed), "pending_open": len(pending), "unknown_total": len(unknown), "missed_entries_confirmed_zero_pnl": len(missed), "no_signal_ticks": len(no_signal)}, "avg_net_r_per_completed_trade": {"value": sum(r_values) / len(r_values) if r_values else None, "denominator": len(r_values)}, "avg_net_r_per_all_eligible_alert": {"value": sum(r_values) / len(executable) if executable and not unknown else None, "denominator": len(executable), "is_undetermined": bool(unknown), "reason": f"{len(unknown)} of {len(executable)} eligible alerts have an unknown outcome" if unknown else None}, "max_drawdown_r_partial_completed_trades_only": max_dd, "max_drawdown_label": "R drawdown on completed trades only — NOT an account-percentage drawdown; PARTIAL whenever unknown_total > 0", "equity_curve": curve, "by_month": {m: v for m, v in sorted(by_month.items())}, "operational": {"decision_delay_seconds_mean": sum(decision_delays) / len(decision_delays) if decision_delays else None, "decision_delay_seconds_max": max(decision_delays) if decision_delays else None, "decision_count": len(decision_delays), "execution_delay_seconds_mean": sum(execution_delays) / len(execution_delays) if execution_delays else None, "execution_delay_seconds_max": max(execution_delays) if execution_delays else None, "quote_samples_recorded": None, "recording_failures": len(failures), "no_signal_ticks": len(no_signal)}, "unobserved": ["Financing/swap charges are not recorded or estimated anywhere in this report.", "Slippage beyond the sampled bid/ask (i.e. the true fill an order would have received) is not observed — this reports the quoted price at the sample that crossed a level, not a broker-confirmed fill.", "Price movement between quote samples (every QUOTE_SAMPLE_INTERVAL_SECONDS) is unobserved and unobservable from this data — a real, stated limitation distinct from the offline replay's continuous M30-candle coverage."]}


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
        # Verify against the frozen run identity without calling report.py's
        # full-log loader.  The manifest already exists, so this is read-only.
        obs_report.load_or_create_manifest(log_dir)
        decisions = list(_iter_jsonl(log_dir / "decisions_log.jsonl"))
        health = list(_iter_jsonl(log_dir / "health_log.jsonl"))
        ledger, quote_rows_scanned, quote_rows_retained = _bounded_ledger(
            obs_score, decisions, log_dir / "quotes_log.jsonl", now,
            obs_contract.QUOTE_STALENESS_SECONDS,
        )
        kpi = _kpi_from_frozen_ledger(ledger, decisions, health, now)
        kpi["operational"]["quote_samples_recorded"] = quote_rows_scanned
        # Keep chronological ordering for the existing dashboard table while
        # retaining the most-recent rows when truncation is necessary.
        recent_ledger = ledger[-RECENT_LEDGER_LIMIT:]

        payload = {
            "generated_at_utc": now.isoformat(),
            "state": "ok",
            "error": None,
            "run_id": manifest.get("run_id"),
            "kpi": kpi,
            "ledger": recent_ledger,
            "ledger_meta": {
                "published_rows": len(recent_ledger), "total_rows": len(ledger),
                "truncated": len(recent_ledger) < len(ledger), "limit": RECENT_LEDGER_LIMIT,
                "quote_rows_retained_for_scoring": quote_rows_retained,
            },
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
                   "run_id": manifest.get("run_id"), "kpi": None, "ledger": None, "ledger_meta": None}
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
