# Phase 3 — WP5 Engineering Assurance

## Bugs found and fixed

### Phase 2 (already documented in `RECONSTRUCTION_VALIDATION_REPORT.md`; restated briefly for completeness)

1. **`as_of`/`now` timing bug** — a fixed `as_of` earlier than real fetch time made `candle_proof()`
   correctly reject every freshly-fetched candle as "from the future." Fixed in `engine.py`'s retry loop by
   advancing (never rewinding) `now` to real wall-clock time after each live fetch.
2. **Request-volume/padding** — fetching only the exact disqualifying gap needed one live request per gap.
   Fixed by padding each fetch (clamped to the source's own per-request span cap) so nearby gaps reuse
   already-cached candles.

### Phase 2B (new in this phase)

3. **Oversized-gap fetch crash.** The Phase 2's padding fix only ever shrinks padding toward 0 as a gap
   widens — it never splits a request when the *raw* gap itself already exceeds
   `OandaPracticeSource.MAX_SPAN_SECONDS` (24,950s). The Phase 2A evidence window (~30h max) never produced
   a gap this wide; Phase 2B's 96h-margin export did (a 176,760s gap on v2-005/006/007). Symptom:
   `historical candle fetch failed: Requested span 176760s exceeds the 24950s per-request bound`. Fixed in
   `engine.py::reconstruct_trade` by chunking any oversized fetch into consecutive bounded sub-requests
   covering the same overall range.
4. **Grid-rounding margin bug**, found immediately after fixing (3): a chunk sized to land exactly on
   24,950s was still rejected (as 24,955s) because `OandaPracticeSource.fetch()` snaps request boundaries
   outward to the nearest 5s grid (floor the start, ceil the end) *before* checking its own span bound.
   Fixed with a 10s safety margin (`SPAN_SAFETY_MARGIN_SECONDS`) applied everywhere `MAX_SPAN_SECONDS` is
   used (both the padding clamp and the chunk-width calculation). Neither fix (3) nor (4) touches
   `research_gap_evidence.py` — both are purely in `engine.py`'s own fetch orchestration.
5. **OOM kill during the first Phase 2B run** (infrastructure, not a scoring bug) — loading the full
   619MB/~1.9M-row extended evidence export into one in-memory dict OOM-killed the process on the 1GB
   droplet, which (because the process was launched from an SSH session) took `ssh.service` down with it
   until systemd recovered it. Confirmed directly via kernel log (`Out of memory: Killed process ...
   anon-rss:429056kB`). Both production observer services remained active throughout, confirmed directly at
   the time and at every subsequent run in this phase. Fixed with a streaming disk-spill
   (`codex_export_adapter.spill_evidence_by_trade` / `load_spilled_trade_evidence`) that never holds more
   than one trade's evidence in memory, plus a `systemd-run --scope -p MemoryMax=300M` isolation layer as a
   second line of defense for every subsequent run in this phase.
6. **Malformed register row.** `V2_REMAINING_GAP_REGISTER.csv` line 493 is a corrupted, partial-duplicate
   fragment of the complete row at line 543 (7 fields instead of the header's 15) — a data-generation
   defect on Codex's side, not something introduced here. `run_v1_targeted_recovery.py` now skips any row
   whose field count doesn't match the header, logging it, instead of crashing or silently misreading it.

## Regression tests

- `test_oversized_gap_is_split_into_bounded_fetches_not_widened_past_the_cap`
  (`tests/test_engine.py`) — reproduces bug (3) and (4) together: a `CapEnforcingSource` that replicates
  `OandaPracticeSource`'s own 5s-grid-snap-then-check-bound behavior, with the gap deliberately placed 4.9s
  off the grid so a margin-0 chunker reproduces the exact real failure (a nominal 24,950s chunk becoming a
  rejected 24,955s request). Verified to fail without the fix and pass with it (checked both ways in this
  session, not merely asserted).
- `test_spill_writes_one_file_per_trade_and_never_builds_one_big_dict`,
  `test_spill_skips_unwanted_trade_ids`, `test_spill_drops_malformed_row_same_as_load_evidence_by_trade`,
  `test_spill_and_in_memory_load_agree_exactly` (`tests/test_codex_export_adapter.py`) — regression for
  bug (5); the last test explicitly asserts the memory-bounded path produces identical output to the old
  in-memory path, confirming this is a pure memory-footprint fix with no behavior change.
- Bugs (1) and (2) have their own existing Phase 2 regression tests
  (`test_fetch_padding_never_exceeds_the_source_span_cap`,
  `test_padding_lets_a_second_nearby_gap_reuse_already_fetched_candles`), unchanged in this phase.
- Bug (6) has no dedicated unit test (it is a defensive length check against a specific real-world file
  defect, not new logic); the real register run in WP3 exercises it directly (1 row skipped, logged, run
  completed successfully).

## Final automated test results

`pytest independent_reconstruction/tests/ -q` → **52 passed**, 0 failed, run immediately before this
document was written.

## Evidence that scoring logic remains unchanged

`research_gap_evidence.py` (the frozen scoring core) has **zero diff** against its Phase 2 commit
(`ef2b49cb3a1439d8163bb5fc8e1e93259b1d1182`, 2026-10-28): `git diff research_gap_evidence.py` is empty, and
its current sha256 is `199cb7634f85ef95a7b1b4b9f4d138706caf4a01c1881a03ed3473ad3a7c66de`. Every fix in this
phase is confined to `engine.py` (fetch orchestration) and `codex_export_adapter.py` (I/O), never the
decision rules themselves. All 7 control/prior-reconstruction trades (v2-003/008/014/015/016 plus
v2-001/002) reproduce their previously-established exit price, exit timestamp (to the microsecond) and R
multiple exactly, both against Phase 2A's own ledger and, for the 4 live controls, against the original
live-observed KPI snapshot — direct, outcome-level evidence that nothing about how a trade is scored
changed, independent of the source-diff check above.

## Evidence that production files and classifications remain untouched

- `git status` (repo-wide) shows changes confined to `independent_reconstruction/*`; no file under
  production paths (the live observer services, their config, their KPI snapshots, or
  `research_gap_evidence.py`) is modified or staged.
- All droplet work ran in `/root/phase2b_scratch/` (Phase 2's own separate `/root/independent_reconstruction_scratch/`
  was used previously and is now superseded), never inside either production service's own directory.
- Both production observer services (`prospective-baseline-observer.service`,
  `prospective-baseline-observer-v2.service`) were directly confirmed `active` before, during (including
  through the OOM incident) and after every run in this phase — never restarted, stopped, or reconfigured.
- Every external evidence file imported (Phase 2B's 5 input files, V1's `quotes_log.jsonl`) was read-only;
  nothing in this phase ever opened those files for writing.

## Reproduction instructions

1. Local tests: `cd independent_reconstruction && python3 -m pytest tests/ -q` (or the repo's `.venv`:
   `./.venv/bin/python3 -m pytest independent_reconstruction/tests/ -q`).
2. Phase 2B reconstruction (requires OANDA practice credentials and network access; fully reproducible from
   cache if the `cache_phase2b/` directory from this run is available):
   ```
   export OANDA_ENVIRONMENT=practice
   export OANDA_API_KEY=<practice key>
   python3 run_phase2b_reconstruction.py
   ```
   Memory-bounded; safe to run under `systemd-run --scope -p MemoryMax=300M` on a constrained host.
3. WP3 V1 targeted recovery (requires the retained `quotes_log.jsonl`; droplet-only, not in this repo):
   `python3 run_v1_targeted_recovery.py`.

## Final artifact hashes (this session)

| File | SHA-256 |
|---|---|
| `engine.py` | `352d7dc90bb35a37c32a182e696d909a90a9a4889ec700273a46c4f0aa31c5d0` |
| `codex_export_adapter.py` | `12cd0ab48104e2dc76c3a35a503cc959d5e490aeaf3989288ca56b96d546eb3a` |
| `v1_targeted_recovery.py` | `bb41ccb50563ebca61e98389a487117f4242a21f01d1939331362bc81f8dd64a` |
| `run_phase2b_reconstruction.py` | `62cb75373ec5edbd13f3870e06f5fe5ca3307bc13a0e984a8b6765dc51c68ab7` |
| `run_v1_targeted_recovery.py` | `e5ab08babe4d999428c0eb3afb1b83ae0c517c6a497276e2bec960d6fefb39af` |
| `output/phase2b_reconstruction_results_v4.json` (final, used throughout this report) | `b3e45a7940be44e144940343cf02646bbc40e93f1882500a5b68c6c93909d8c6` |
| `output/v1_targeted_recovery_results.json` | `e4b47614b31753352a6cbe782d1302c176ee5ec7f173a5c8c1a98b94f0d3c207` |
| `research_gap_evidence.py` (unchanged, frozen) | `199cb7634f85ef95a7b1b4b9f4d138706caf4a01c1881a03ed3473ad3a7c66de` |