"""Adapts Codex's real phase-2 export (V2_RECONSTRUCTION_INPUT.csv,
V2_OBSERVATION_EVIDENCE.csv -- actual schema, confirmed by inspection on
2026-11-24) into the internal shapes engine.reconstruct_trade() and
research_gap_evidence.reconstruct() already expect. No change to either of
those modules; this is purely an input-mapping layer. Read-only -- never
writes to the files it's given.

Schema differences from the guessed schema documented in
RECONSTRUCTION_METHODOLOGY.md (written before the real export existed):
  trade_id              -> decision_id
  entry_timestamp_utc   -> assumed_entry_time_utc
  entry_price           -> assumed_entry_price
  stop_loss             -> stop
  original_deadline_utc -> inverted into max_holding_time_hours (exact
                            arithmetic inversion of Codex's own authoritative
                            deadline, not a re-derived assumption -- see
                            _hours_between below)
  existing_outcome_classification -> original_state
There is no original_exit_time_utc/original_exit_price/original_r_multiple
column in the real input file at all -- those are pulled separately from
source_research_kpi_v2.json's own ledger (the authoritative KPI snapshot
Codex captured alongside the export) by load_original_outcomes().

The evidence file is already PRE-WINDOWED per trade_id (confirmed: every
one of its 452,706 rows carries the owning trade_id, no row belongs to
more than one trade's window) -- so this adapter streams it once and
buckets by trade_id directly, rather than recomputing decision windows
itself the way the production publisher does for its own, unwindowed log.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import research_gap_evidence as rge  # noqa: E402

SUPPRESSED_OR_NOT_ENTERED = {"suppressed_existing_position", "insufficient_data_entry"}


def _hours_between(entry_iso: str, deadline_iso: str) -> float:
    entry, deadline = rge._time(entry_iso), rge._time(deadline_iso)
    if entry is None or deadline is None:
        raise ValueError("unparseable entry or deadline timestamp")
    return (deadline - entry).total_seconds() / 3600.0


def load_trades(path: Path) -> list[dict]:
    """Returns one dict per ENTERED trade only (rows with a non-blank
    trade_id) -- suppressed and never-entered rows are real, meaningful
    ledger rows (reconciled separately, see the input-validation report)
    but are not reconstruction candidates and are excluded here, not
    silently miscounted as entered."""
    trades = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            trade_id = raw.get("trade_id", "").strip()
            if not trade_id:
                continue  # suppressed_existing_position / insufficient_data_entry row
            errors = []
            try:
                stop = float(raw["stop_loss"])
                target = float(raw["target"])
                entry_price = float(raw["entry_price"])
            except (KeyError, ValueError, TypeError):
                errors.append("entry_price/stop_loss/target missing or not a finite number")
                stop = target = entry_price = None
            try:
                max_holding_time_hours = _hours_between(raw["entry_timestamp_utc"], raw["original_deadline_utc"])
                if max_holding_time_hours <= 0:
                    errors.append("original_deadline_utc is not after entry_timestamp_utc")
            except (KeyError, ValueError):
                errors.append("entry_timestamp_utc/original_deadline_utc missing or unparseable")
                max_holding_time_hours = None
            if raw.get("direction") not in ("long", "short"):
                errors.append(f"direction {raw.get('direction')!r} is not 'long' or 'short'")
            if raw.get("measurement_contract_version") != "v2":
                errors.append(f"measurement_contract_version {raw.get('measurement_contract_version')!r} != 'v2'")
            if not errors and stop is not None and None not in (entry_price, target):
                long = raw["direction"] == "long"
                if not ((stop < entry_price < target) if long else (target < entry_price < stop)):
                    errors.append("stop/entry/target geometry is inconsistent with direction")
            trades.append({
                "decision_id": trade_id, "trade_id": trade_id, "pair": raw.get("pair"),
                "direction": raw.get("direction"),
                "assumed_entry_time_utc": raw.get("entry_timestamp_utc"),
                "assumed_entry_price": entry_price, "stop": stop, "target": target,
                "max_holding_time_hours": max_holding_time_hours,
                "original_state": raw.get("existing_outcome_classification"),
                "original_deadline_utc": raw.get("original_deadline_utc"),
                "adjusted_deadline_utc": raw.get("adjusted_deadline_utc") or None,
                "_errors": errors,
            })
    return trades


def load_evidence_by_trade(path: Path, wanted_trade_ids: set[str] | None = None) -> dict[str, list[dict]]:
    """One streaming pass over the 90MB export, bucketing rows by trade_id
    in memory (not per-pair re-scanning, and no temp-file spill -- this is
    a one-off local analysis run, not the production publisher's bounded
    droplet path; see HISTORICAL_DATA_MANIFEST.md for why that's an
    acceptable tradeoff here). wanted_trade_ids, if given, skips rows for
    any other trade without even constructing a dict for them."""
    out: dict[str, list[dict]] = {}
    dropped = 0
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            tid = raw.get("trade_id", "").strip()
            if not tid or (wanted_trade_ids is not None and tid not in wanted_trade_ids):
                continue
            try:
                bid, ask = float(raw["bid"]), float(raw["ask"])
                tradeable = raw["tradeable"].strip().lower() == "true"
                received, provider = rge._time(raw["received_at_utc"]), rge._time(raw["oanda_time_utc"])
                if received is None or provider is None or provider > received or bid > ask:
                    dropped += 1
                    continue
            except (KeyError, ValueError):
                dropped += 1
                continue
            out.setdefault(tid, []).append({
                "pair": raw["pair"], "received_at_utc": raw["received_at_utc"],
                "oanda_time_utc": raw["oanda_time_utc"], "bid": bid, "ask": ask, "tradeable": tradeable,
            })
    return out


def load_original_outcomes(kpi_snapshot_path: Path, trades: list[dict]) -> dict[str, dict]:
    """Pulls original exit_time_utc/exit_price/r_multiple for the
    resolved trades from the captured KPI snapshot's own ledger (the
    input CSV has no exit columns at all) -- matched by pair + exact
    assumed_entry_time_utc, the same identity key the live dashboard's own
    research_ledger_display() uses, never by position/order."""
    kpi = json.loads(kpi_snapshot_path.read_text())
    by_key = {}
    for row in kpi.get("ledger", []):
        if not row.get("executable") or not row.get("assumed_entry_time_utc"):
            continue
        key = (row["pair"], row["assumed_entry_time_utc"])
        by_key[key] = row

    outcomes = {}
    for t in trades:
        key = (t["pair"], t["assumed_entry_time_utc"])
        row = by_key.get(key)
        if row is None:
            outcomes[t["trade_id"]] = {"found": False}
            continue
        outcomes[t["trade_id"]] = {
            "found": True, "state": row.get("state"),
            "exit_time_utc": row.get("exit_time_utc"), "exit_price": row.get("exit_price"),
            "r_multiple": row.get("r_multiple"), "deadline_delay_reason": row.get("deadline_delay_reason"),
        }
    return outcomes
