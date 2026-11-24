"""Builds the exact register_rows needed for v1_targeted_recovery.run():
every "not extracted" row (v2-020's 49 mid-scan gaps, v2-022's 1
ambiguous trailing gap) PLUS a small independent-verification sample
drawn from rows Codex already marked as extracted, spanning different
pairs/dates, to spot-check trustworthiness rather than accept it
blindly. Runs on the droplet (V1's log lives there only); writes a
small JSON result, never touches the original register or any V2 file.
"""
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import v1_targeted_recovery as v1r

REGISTER = Path(__file__).resolve().parent / "input" / "phase2b" / "V2_REMAINING_GAP_REGISTER.csv"

# Independent-verification sample: different pairs/dates than v2-020/v2-022,
# chosen from rows Codex already reports as extracted, so the result here
# can be directly compared against the register's own claimed values.
VERIFY_SAMPLE_KEYS = {
    ("v2-001", "1"), ("v2-003", "1"), ("v2-008", "1"), ("v2-015", "1"),
}


def main():
    not_extracted = []
    verify_sample = []
    malformed = 0
    with REGISTER.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        for i, row in enumerate(reader, start=2):
            # The register has one known-malformed line (a corrupted,
            # partial-duplicate fragment of a legitimate row elsewhere in
            # the file -- confirmed by direct inspection on 2026-11-24;
            # see PHASE3_BASELINE_RECONCILIATION.md). It carries no usable
            # trade_id/gap_number and is skipped, counted, never guessed at.
            if len(row) != len(header):
                malformed += 1
                print(f"skipping malformed register row at line {i}: len={len(row)} != {len(header)}: {row}",
                      file=sys.stderr)
                continue
            trade_id, pair, gap_number, gap_phase, start_iso, end_iso = row[0], row[1], row[2], row[3], row[4], row[5]
            v1_col = row[8]
            if "not extracted" in v1_col:
                not_extracted.append((trade_id, pair, gap_number, gap_phase, start_iso, end_iso))
            elif (trade_id, gap_number) in VERIFY_SAMPLE_KEYS:
                verify_sample.append((trade_id, pair, gap_number, gap_phase, start_iso, end_iso,
                                      row[9], row[10], row[11], row[12]))  # claimed v1 columns

    print(f"malformed rows skipped: {malformed}", file=sys.stderr)
    print(f"not_extracted rows: {len(not_extracted)}", file=sys.stderr)
    print(f"verify_sample rows: {len(verify_sample)}", file=sys.stderr)

    all_targets = [(t, p, g, ph, s, e) for (t, p, g, ph, s, e) in not_extracted] + \
                  [(t, p, g, ph, s, e) for (t, p, g, ph, s, e, *_rest) in verify_sample]

    intervals_by_pair = v1r.load_intervals(all_targets)
    t0 = time.monotonic()
    results, rows_scanned, pair_rows_checked = v1r.run(intervals_by_pair)
    elapsed = time.monotonic() - t0

    out = {
        "elapsed_seconds": elapsed, "v1_rows_scanned": rows_scanned,
        "v1_rows_matching_target_pairs": pair_rows_checked,
        "not_extracted_results": {},
        "verification_sample_results": {},
    }
    for trade_id, pair, gap_number, gap_phase, start_iso, end_iso in not_extracted:
        key = f"{trade_id}#{gap_number}"
        out["not_extracted_results"][key] = {
            "trade_id": trade_id, "pair": pair, "gap_number": gap_number, "gap_phase": gap_phase,
            "gap_start_utc": start_iso, "gap_end_utc": end_iso,
            "v1_first_observation": results.get((trade_id, gap_number)),
        }
    for trade_id, pair, gap_number, gap_phase, start_iso, end_iso, claimed_n, claimed_first_t, claimed_bid, claimed_ask in verify_sample:
        key = f"{trade_id}#{gap_number}"
        out["verification_sample_results"][key] = {
            "trade_id": trade_id, "pair": pair, "gap_number": gap_number,
            "claimed_by_codex": {"v1_observations_in_gap": claimed_n, "first_observation_utc": claimed_first_t,
                                 "first_bid": claimed_bid, "first_ask": claimed_ask},
            "independently_found": results.get((trade_id, gap_number)),
        }

    out_path = Path(__file__).resolve().parent / "output" / "v1_targeted_recovery_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"elapsed={elapsed:.2f}s v1_rows_scanned={rows_scanned} pair_rows_checked={pair_rows_checked}", file=sys.stderr)
    print(f"wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
