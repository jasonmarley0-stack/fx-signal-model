"""Bounded, separate audit of existing v2 entries. Never writes live KPIs.

Default: offline, no network. --fetch-gap-candles explicitly enables bounded
OANDA practice GETs for ONLY unresolved gap intervals. No orders/endpoints.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import research_snapshot_publisher as pub
from research_gap_evidence import CONTRACT, reconstruct
from research_quote_index import QuoteIndex

PACKAGE = "research/prospective_baseline_v2"
MAX_REQUESTS_PER_DECISION = 20


def fetch_gap_candles(pair, start, end, evidence_dir, request_fn=None, now_fn=None):
    # Enforced BEFORE constructing a client or making a request.
    if os.environ.get("OANDA_ENVIRONMENT") != "practice":
        raise RuntimeError("Gap evidence fetching requires OANDA_ENVIRONMENT=practice.")
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    a = datetime.fromtimestamp(math.floor(start.timestamp() / 5) * 5, timezone.utc)
    b = datetime.fromtimestamp(math.ceil(end.timestamp() / 5) * 5, timezone.utc)
    if (b - a).total_seconds() > 4990 * 5:
        return []  # no unbounded history request; stays unknown
    params = {"from": a.isoformat(), "to": b.isoformat(), "price": "BA", "granularity": "S5"}
    instrument = pair[:3] + "_" + pair[3:]
    if request_fn is None:
        from oandapyV20 import API
        from oandapyV20.endpoints.instruments import InstrumentsCandles
        key = os.environ.get("OANDA_API_KEY")
        if not key:
            raise RuntimeError("OANDA practice credentials are not configured.")
        api = API(access_token=key, environment="practice", request_params={"timeout": 8})
        request_fn = lambda instr, query: api.request(InstrumentsCandles(instrument=instr, params=query))
    # Errors deliberately propagate to a generic, path/credential-free state
    # in audit(); no exception body containing request context is published.
    response = request_fn(instrument, params)
    fetched = now_fn()
    evidence = {"provider": "OANDA practice", "instrument": instrument, "params": params,
                "fetched_at_utc": fetched.isoformat(), "response": response}
    raw = json.dumps(evidence, sort_keys=True, allow_nan=False)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    target = evidence_dir / (digest + ".json")
    # Content-addressed, append-only evidence owned by this audit, not observer.
    if not target.exists():
        with target.open("x") as f:
            f.write(raw)
    bars = []
    for c in response.get("candles", []):
        if c.get("complete") is not True:
            continue
        try:
            t = datetime.fromisoformat(c["time"])
            if t.tzinfo is None:
                continue
            for side in ("bid", "ask"):
                px = c[side]
                bars.append({"pair": pair, "side": side, "complete": True,
                             "start_utc": t.isoformat(), "end_utc": (t + timedelta(seconds=5)).isoformat(),
                             "open": float(px["o"]), "high": float(px["h"]), "low": float(px["l"]),
                             "close": float(px["c"]), "fetched_at_utc": fetched.isoformat(),
                             "evidence_sha256": digest})
        except (KeyError, TypeError, ValueError):
            continue
    return bars


def audit(checkout, out_dir, fetch=False):
    if out_dir.resolve().is_relative_to(checkout.resolve()):
        raise RuntimeError("Audit output must be outside the observer checkout.")
    logs = checkout / PACKAGE / "logs"
    manifest = pub._read_manifest(logs)
    if manifest is None:
        raise RuntimeError("Existing observer manifest required; audit never creates one.")
    contract, scorer, report = pub._import_observer_modules(checkout, PACKAGE)
    report.load_or_create_manifest(logs)  # already exists: verifies only
    now = datetime.now(timezone.utc)
    decisions = list(pub._iter_jsonl(logs / "decisions_log.jsonl"))
    cache = out_dir / "evidence_quote_index.sqlite3"
    frozen, _, _ = pub._bounded_ledger(scorer, decisions, logs / "quotes_log.jsonl", now,
                                     pub._coverage_margin_seconds(contract), pub._closure_margin_seconds(contract), cache)
    index = QuoteIndex(cache)
    derived = []
    try:
        for row in frozen:
            if not row.get("executable") or not row.get("assumed_entry_time_utc"):
                continue
            start = datetime.fromisoformat(row["assumed_entry_time_utc"])
            end = min(now, start + timedelta(hours=float(row["max_holding_time_hours"]) + 60, seconds=15))
            quotes = index.window(row["pair"], start, end)
            candles, fetched_gaps = [], set()
            result = reconstruct(row, quotes, candles, now)
            for _ in range(MAX_REQUESTS_PER_DECISION if fetch else 0):
                gap = result.get("first_unproved_gap")
                if result["state"] != "unknown" or gap is None:
                    break
                key = (gap["from_utc"], gap["to_utc"])
                if key in fetched_gaps:
                    break  # still ambiguous/missing despite archive: do not force it
                fetched_gaps.add(key)
                try:
                    candles.extend(fetch_gap_candles(row["pair"], datetime.fromisoformat(key[0]),
                                                     datetime.fromisoformat(key[1]), out_dir / "candle_evidence"))
                except Exception:
                    result["evidence_fetch_status"] = "failed; no quote or outcome fabricated"
                    break
                # This audit is retrospective. fetched_at is recorded, and no
                # pre-fetch point-in-time validation claim is made.
                now = datetime.now(timezone.utc)
                index.sync(logs / "quotes_log.jsonl")
                end = min(now, start + timedelta(hours=float(row["max_holding_time_hours"]) + 60, seconds=15))
                quotes = index.window(row["pair"], start, end)
                result = reconstruct(row, quotes, candles, now)
            result.update(pair=row["pair"], assumed_entry_time_utc=row["assumed_entry_time_utc"],
                          original_r_multiple=row.get("r_multiple"), archive_requests=len(fetched_gaps))
            derived.append(result)
    finally:
        index.close()
    payload = {"state": "ok", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
               "run_id": manifest["run_id"], "source_hash": manifest["source_hash"],
               "measurement_contract": CONTRACT, "audit_source_hash": audit_source_hash(),
               "live_kpi_unchanged": True, "network_enabled": fetch,
               "limitation": "Supplementary reconstruction of fixed original entries/suppression, NOT a newly executed strategy or fresh prospective validation.",
               "ledger": derived}
    pub._atomic_write_json(out_dir / "research_supplementary_evidence.json", payload)
    return payload


def audit_source_hash():
    h = hashlib.sha256()
    for name in ("research_evidence_audit.py", "research_gap_evidence.py", "research_quote_index.py", "research_snapshot_publisher.py"):
        h.update(name.encode())
        h.update(Path(__file__).with_name(name).read_bytes())
    return h.hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observer-checkout", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--fetch-gap-candles", action="store_true")
    args = parser.parse_args()
    # Output/evidence/index must never be written inside the frozen checkout.
    if args.out_dir.resolve().is_relative_to(args.observer_checkout.resolve()):
        parser.error("Output must be outside the observer checkout.")
    payload = audit(args.observer_checkout, args.out_dir, args.fetch_gap_candles)
    print(json.dumps({"state": payload["state"], "entered_rows_audited": len(payload["ledger"]),
                      "measurement_contract": CONTRACT, "live_kpi_unchanged": True}))
