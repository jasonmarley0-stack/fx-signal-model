"""Standalone, CSV-driven independent reconstruction engine for the 22
entered v2 paper trades. Reuses the already-proven decision rules from
research_gap_evidence.reconstruct() (candle-envelope no-touch proofs,
continuous-non-tradeability bridging, side convention, stop/target
precedence, conservative insufficient-evidence refusal) unmodified --
those rules were already validated against the real frozen v2 scorer and
real production data; this module does not re-derive them.

Never touches the production observer, the live KPI snapshot, or any
deployed service. Reads only the CSV/manifest files passed in.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, for research_gap_evidence
import research_gap_evidence as rge  # noqa: E402  (the already-proven rules)

from historical_sources import HistoricalSource, OandaPracticeSource  # noqa: E402

RESOLVED_ORIGINAL_STATES = {"stopped", "targeted", "time_exited"}
REQUIRED_INPUT_FIELDS = ["decision_id", "pair", "direction", "assumed_entry_time_utc",
                          "assumed_entry_price", "stop", "target", "max_holding_time_hours",
                          "original_state"]
REQUIRED_EVIDENCE_FIELDS = ["pair", "received_at_utc", "oanda_time_utc", "bid", "ask", "tradeable"]
SCOPED_PAIRS = {"EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF", "NZDUSD", "AUDUSD"}

CLASSIFICATION_MAP = {
    "targeted": "reconstructed_targeted",
    "stopped": "reconstructed_stopped",
    "time_exited": "reconstructed_time_exit",
}


class ContractIncompatibilityError(RuntimeError):
    """Raised when an input file does not match the documented schema --
    never silently reinterpreted. See RECONSTRUCTION_METHODOLOGY.md section 2."""


def _parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in ("true", "1"):
        return True
    if v in ("false", "0"):
        return False
    raise ValueError(f"not a boolean: {value!r}")


def _parse_float(value: str) -> float:
    f = float(value)
    if not math.isfinite(f):
        raise ValueError(f"not finite: {value!r}")
    return f


def load_trades(path: Path) -> list[dict]:
    """Loads and strictly validates V2_RECONSTRUCTION_INPUT.csv. Raises
    ContractIncompatibilityError (naming the row and field) rather than
    silently dropping or defaulting a bad row -- callers decide whether
    that row becomes unresolved_measurement_contract_incompatibility or the
    whole file is rejected."""
    rows = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        missing = set(REQUIRED_INPUT_FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ContractIncompatibilityError(
                f"{path}: missing required column(s) {sorted(missing)}")
        for i, raw in enumerate(reader):
            row = dict(raw)
            row["_row_index"] = i
            row["_errors"] = []
            if not row.get("decision_id", "").strip():
                row["_errors"].append("decision_id is blank")
            if row.get("pair") not in SCOPED_PAIRS:
                row["_errors"].append(f"pair {row.get('pair')!r} is not one of the scoped pairs")
            if row.get("direction") not in ("long", "short"):
                row["_errors"].append(f"direction {row.get('direction')!r} is not 'long' or 'short'")
            entry_dt = rge._time(row.get("assumed_entry_time_utc"))
            if entry_dt is None:
                row["_errors"].append("assumed_entry_time_utc is missing, malformed or not tz-aware")
            for field_name in ("assumed_entry_price", "stop", "target"):
                try:
                    v = _parse_float(row[field_name])
                    if v <= 0:
                        row["_errors"].append(f"{field_name} must be positive, got {v}")
                    row[field_name] = v
                except (KeyError, ValueError):
                    row["_errors"].append(f"{field_name} is missing or not a finite number")
            try:
                h = _parse_float(row["max_holding_time_hours"])
                if h <= 0:
                    row["_errors"].append("max_holding_time_hours must be positive")
                row["max_holding_time_hours"] = h
            except (KeyError, ValueError):
                row["_errors"].append("max_holding_time_hours is missing or not a finite number")
            if row.get("original_state") in RESOLVED_ORIGINAL_STATES:
                for f_name in ("original_exit_time_utc", "original_exit_price", "original_r_multiple"):
                    if not row.get(f_name, "").strip():
                        row["_errors"].append(f"original_state is resolved but {f_name} is blank")
            if not row["_errors"] and not (
                    (row["stop"] < row["assumed_entry_price"] < row["target"]) if row["direction"] == "long"
                    else (row["target"] < row["assumed_entry_price"] < row["stop"])):
                row["_errors"].append("stop/entry/target geometry is inconsistent with direction")
            row["assumed_entry_time_utc"] = entry_dt.isoformat() if entry_dt else row.get("assumed_entry_time_utc")
            rows.append(row)
    return rows


def load_evidence(path: Path) -> list[dict]:
    """Loads V2_OBSERVATION_EVIDENCE.csv. A malformed row is dropped (never
    read as a zero/default value) and counted, never silently treated as
    absent-of-evidence for scoring purposes beyond simply not being usable."""
    rows, dropped = [], 0
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        missing = set(REQUIRED_EVIDENCE_FIELDS) - set(reader.fieldnames or [])
        if missing:
            raise ContractIncompatibilityError(
                f"{path}: missing required column(s) {sorted(missing)}")
        for raw in reader:
            try:
                row = {
                    "pair": raw["pair"],
                    "received_at_utc": raw["received_at_utc"],
                    "oanda_time_utc": raw["oanda_time_utc"],
                    "bid": _parse_float(raw["bid"]),
                    "ask": _parse_float(raw["ask"]),
                    "tradeable": _parse_bool(raw["tradeable"]),
                }
                received, provider = rge._time(row["received_at_utc"]), rge._time(row["oanda_time_utc"])
                if received is None or provider is None or provider > received or row["bid"] > row["ask"]:
                    dropped += 1
                    continue
                rows.append(row)
            except (KeyError, ValueError):
                dropped += 1
                continue
    return rows


@dataclass
class ReconstructionResult:
    decision_id: str
    pair: str
    original_state: str
    classification: str
    exit_time_utc: str | None = None
    exit_price: float | None = None
    r_multiple: float | None = None
    reason: str | None = None
    proofs: list = field(default_factory=list)
    is_control: bool = False  # True for already-resolved originals, replayed for section 7


def _classify(raw_result: dict) -> str:
    state = raw_result.get("state")
    if state in CLASSIFICATION_MAP:
        return CLASSIFICATION_MAP[state]
    reason = (raw_result.get("reason") or "")
    if "ambiguous" in reason.lower() or state == "ambiguous_intrabar_exit":
        return "unresolved_ambiguous_price_sequence"
    return "unresolved_insufficient_historical_data"


def reconstruct_trade(trade: dict, evidence: list[dict], source: HistoricalSource | None,
                       now: datetime, max_requests: int = 20) -> ReconstructionResult:
    decision_id = trade["decision_id"]
    pair = trade["pair"]
    is_control = trade.get("original_state") in RESOLVED_ORIGINAL_STATES

    if trade["_errors"]:
        return ReconstructionResult(decision_id, pair, trade.get("original_state", ""),
                                    "unresolved_missing_original_trade_parameters",
                                    reason="; ".join(trade["_errors"]), is_control=is_control)

    quotes = [q for q in evidence if q["pair"] == pair]
    candles: list[dict] = []
    fetched_gaps: set[tuple] = set()
    result = rge.reconstruct(trade, quotes, candles, now)
    for _ in range(max_requests if source is not None else 0):
        gap = result.get("first_unproved_gap")
        if result["state"] != "unknown" or gap is None:
            break
        key = (gap["from_utc"], gap["to_utc"])
        if key in fetched_gaps:
            break
        fetched_gaps.add(key)
        try:
            candles.extend(source.fetch(pair, datetime.fromisoformat(key[0]), datetime.fromisoformat(key[1])))
        except Exception as exc:
            return ReconstructionResult(decision_id, pair, trade["original_state"],
                                        "unresolved_insufficient_historical_data",
                                        reason=f"historical candle fetch failed: {exc}",
                                        proofs=result.get("proofs", []), is_control=is_control)
        now = datetime.now(timezone.utc) if now is None else now
        result = rge.reconstruct(trade, quotes, candles, now)

    classification = _classify(result)
    return ReconstructionResult(decision_id, pair, trade["original_state"], classification,
                                exit_time_utc=result.get("exit_time_utc"), exit_price=result.get("exit_price"),
                                r_multiple=result.get("r_multiple"), reason=result.get("reason"),
                                proofs=result.get("proofs", []), is_control=is_control)


def run(trades: list[dict], evidence: list[dict], source: HistoricalSource | None,
        as_of: datetime, max_requests_per_trade: int = 20) -> list[ReconstructionResult]:
    """Deterministic given the same inputs, the same as_of, and the same
    (cached) source responses -- iterates trades in the order they were
    loaded, no randomness, no implicit wall-clock reads."""
    return [reconstruct_trade(t, evidence, source, as_of, max_requests_per_trade) for t in trades]


def write_ledger_csv(results: list[ReconstructionResult], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["decision_id", "pair", "original_state", "classification",
                                               "exit_time_utc", "exit_price", "r_multiple", "reason", "is_control"])
        writer.writeheader()
        for r in results:
            writer.writerow({"decision_id": r.decision_id, "pair": r.pair, "original_state": r.original_state,
                             "classification": r.classification, "exit_time_utc": r.exit_time_utc,
                             "exit_price": r.exit_price, "r_multiple": r.r_multiple, "reason": r.reason,
                             "is_control": r.is_control})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path(__file__).parent / "cache")
    parser.add_argument("--as-of", type=str, required=True, help="ISO-8601 UTC timestamp; fixed for determinism")
    parser.add_argument("--fetch-historical-candles", action="store_true")
    parser.add_argument("--max-requests-per-trade", type=int, default=20)
    args = parser.parse_args()

    as_of = datetime.fromisoformat(args.as_of)
    if as_of.tzinfo is None:
        parser.error("--as-of must be tz-aware (include a UTC offset)")

    trades = load_trades(args.input)
    evidence = load_evidence(args.evidence)
    source = None
    if args.fetch_historical_candles:
        import os
        source = OandaPracticeSource(args.cache_dir, api_key=os.environ.get("OANDA_API_KEY"))

    results = run(trades, evidence, source, as_of, args.max_requests_per_trade)
    write_ledger_csv(results, args.out_dir / "V2_RECONSTRUCTED_TRADE_LEDGER.csv")
    counts = {}
    for r in results:
        counts[r.classification] = counts.get(r.classification, 0) + 1
    print(json.dumps({"trades": len(results), "classifications": counts}))


if __name__ == "__main__":
    main()
