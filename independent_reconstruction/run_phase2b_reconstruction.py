"""Phase 2B driver: reruns all 22 entered trades against the EXTENDED
evidence export (96h post-deadline margin) instead of Phase 2A's
nominal-deadline-cutoff export. Same engine, same frozen rules, same
trade definitions (V2_RECONSTRUCTION_INPUT.csv unchanged) -- only the
evidence file differs. Writes a separate output file; Phase 2A's
results are never overwritten.

MEMORY: the extended export is 619MB / ~1.9M rows (96h margin per trade
vs Phase 2A's ~30h) -- loading that into one in-memory dict is what
OOM-killed this exact script on 2026-11-24 (confirmed via kernel log:
`Out of memory: Killed process ... python3 ... anon-rss:429056kB`, and
because the process was backgrounded from an SSH session it was still
accounted inside ssh.service's own cgroup, so the kill took the SSH
service itself down with it until systemd recovered it -- production
observers were never affected). Fixed by spilling to one small
per-trade file on disk in a single streaming pass
(codex_export_adapter.spill_evidence_by_trade), then reading back one
trade's file at a time -- never all 22 trades' evidence in memory at once.
"""
import json
import os
import sys
import tempfile
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codex_export_adapter as adapter
import engine as eng
from historical_sources import OandaPracticeSource

DATA_DIR = Path(__file__).resolve().parent / "data"
PHASE2B_DIR = Path(__file__).resolve().parent / "input" / "phase2b"
AS_OF = datetime.now(timezone.utc)  # see Phase 2's own fix: must be >= any live fetch's real time


def main():
    trades = adapter.load_trades(DATA_DIR / "V2_RECONSTRUCTION_INPUT.csv")
    trade_ids = {t["trade_id"] for t in trades}
    outcomes = adapter.load_original_outcomes(DATA_DIR / "source_research_kpi_v2.json", trades)

    cache_dir = Path(__file__).resolve().parent / "cache_phase2b"
    source = OandaPracticeSource(cache_dir, api_key=os.environ.get("OANDA_API_KEY"))

    spill_dir = Path(tempfile.mkdtemp(prefix="phase2b_evidence_spill_"))
    print(f"spilling evidence to {spill_dir} (one streaming pass, one file per trade)...", file=sys.stderr)
    spill_paths = adapter.spill_evidence_by_trade(
        PHASE2B_DIR / "V2_EXTENDED_OBSERVATION_EVIDENCE.csv", trade_ids, spill_dir)

    results = []
    for t in trades:
        quotes = adapter.load_spilled_trade_evidence(spill_paths[t["trade_id"]])  # one trade's share only
        r = eng.reconstruct_trade(t, quotes, source, AS_OF, max_requests=20)
        row_count = len(quotes)
        quotes = None  # drop this trade's evidence before moving to the next
        d = asdict(r)
        d["trade_id"] = t["trade_id"]
        d["direction"] = t["direction"]
        d["assumed_entry_time_utc"] = t["assumed_entry_time_utc"]
        d["assumed_entry_price"] = t["assumed_entry_price"]
        d["stop"] = t["stop"]
        d["target"] = t["target"]
        d["max_holding_time_hours"] = t["max_holding_time_hours"]
        d["deadline_utc"] = (datetime.fromisoformat(t["assumed_entry_time_utc"])
                             + timedelta(hours=t["max_holding_time_hours"])).isoformat()
        d["original_outcome"] = outcomes.get(t["trade_id"])
        d["evidence_row_count"] = row_count
        results.append(d)
        print(f"{t['trade_id']} {t['pair']:7} {t['direction']:5} "
              f"original={t['original_state']:12} -> {r.classification} (evidence_rows={row_count})",
              file=sys.stderr)

    out_path = Path(__file__).resolve().parent / "output" / "phase2b_reconstruction_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"as_of_utc": AS_OF.isoformat(), "evidence_source": "V2_EXTENDED_OBSERVATION_EVIDENCE.csv",
                                    "results": results}, indent=2, default=str))
    print(f"wrote {out_path}", file=sys.stderr)

    for p in spill_paths.values():
        p.unlink(missing_ok=True)
    spill_dir.rmdir()
    print(f"cleaned up {spill_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
