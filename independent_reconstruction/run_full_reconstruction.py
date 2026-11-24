"""Driver: reconstructs all 22 entered v2 trades (4 controls + 18
originally-unresolved, including the 2 already-supplementarily-
reconstructed USDJPY ones) using the real Codex export. Designed to run
on the droplet (where OANDA credentials live) against a scratch copy of
the data -- never touches any production path. Writes one JSON file with
every result; the caller pulls that back plus the (non-secret) OANDA
candle cache for local analysis.
"""
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import codex_export_adapter as adapter
import engine as eng
from historical_sources import OandaPracticeSource

DATA_DIR = Path(__file__).resolve().parent / "data"
# Must be >= the real wall-clock moment any candle gets fetched: candle_proof()
# correctly rejects a bar whose own fetched_at_utc is after `now` (never use
# information "from the future" relative to the scoring instant) -- freezing
# this to the captured KPI snapshot's much-earlier generated_at_utc would
# reject every freshly-fetched candle outright. Captured once, at the start
# of this run, and reused for every trade -- reproducibility comes from
# recording this exact value in the output (done below) plus the on-disk
# candle cache, not from hardcoding a value from a different moment.
AS_OF = datetime.now(timezone.utc)


def main():
    trades = adapter.load_trades(DATA_DIR / "V2_RECONSTRUCTION_INPUT.csv")
    trade_ids = {t["trade_id"] for t in trades}
    evidence_by_trade = adapter.load_evidence_by_trade(DATA_DIR / "V2_OBSERVATION_EVIDENCE.csv", trade_ids)
    outcomes = adapter.load_original_outcomes(DATA_DIR / "source_research_kpi_v2.json", trades)

    cache_dir = Path(__file__).resolve().parent / "cache"
    source = OandaPracticeSource(cache_dir, api_key=os.environ.get("OANDA_API_KEY"))

    results = []
    for t in trades:
        quotes = evidence_by_trade.get(t["trade_id"], [])
        r = eng.reconstruct_trade(t, quotes, source, AS_OF, max_requests=20)
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
        d["evidence_row_count"] = len(quotes)
        results.append(d)
        print(f"{t['trade_id']} {t['pair']:7} {t['direction']:5} "
              f"original={t['original_state']:12} -> {r.classification}", file=sys.stderr)

    out_path = Path(__file__).resolve().parent / "output" / "full_reconstruction_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"as_of_utc": AS_OF.isoformat(), "results": results}, indent=2, default=str))
    print(f"wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
