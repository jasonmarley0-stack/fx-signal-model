# Reconstruction Validation Report

Run against the real Codex export (`V2_RECONSTRUCTION_INPUT.csv`, `V2_OBSERVATION_EVIDENCE.csv`, hashes
verified against `EVIDENCE_MANIFEST.md` before use), 2026-11-24. `as_of_utc` for this run:
see `output/full_reconstruction_results.json`. Reproducible from the same files plus the
content-addressed OANDA candle cache under `cache/`.

## Pre-registered tolerances (fixed before examining results)

- Exit price: exact match to 5 decimal places (OANDA's own quoted precision).
- Exit timestamp: exact match to the second.
- R multiple: exact match within `1e-9` relative tolerance (floating-point only).

## Live-completed control validation (4 trades)

| Trade | Pair | Original (live) | Independently reconstructed | Result |
|---|---|---|---|---|
| v2-008 | USDJPY long | targeted, exit 158.525 @ 2026-11-03T15:17:18.168919Z, R=+1.072464 | targeted, exit 158.525 @ 2026-11-03T15:17:18.168919Z, R=+1.072464 | **EXACT MATCH** |
| v2-015 | EURUSD short | targeted, exit 1.1123 @ 2026-11-11T13:17:01.882877Z, R=+0.892649 | targeted, exit 1.1123 @ 2026-11-11T13:17:01.882877Z, R=+0.892649 | **EXACT MATCH** |
| v2-016 | USDJPY long | targeted, exit 159.542 @ 2026-11-11T13:49:53.282780Z, R=+0.955631 | targeted, exit 159.542 @ 2026-11-11T13:49:53.282780Z, R=+0.955631 | **EXACT MATCH** |
| v2-003 | USDCAD short | time_exited (closure-delayed), exit 1.40487 @ 2026-10-25T21:04:56.913807Z, R=−0.754115 | **Unresolved** — "Awaiting an observed tradeable reopening." | **VALIDATION GAP** |

**3 of 4 controls validate exactly** — byte-for-byte on exit price, exit timestamp, and R, not merely
"close." This confirms the reused `research_gap_evidence.py` rules (candle-envelope proof, side
convention, stop/target precedence) reproduce the frozen scorer's own logic correctly for every
mid-scan-gap case these three needed.

### v2-003: root cause of the validation gap (confirmed, not assumed)

`V2_OBSERVATION_EVIDENCE.csv`'s per-trade window for v2-003 ends exactly at `nominal_deadline_utc`
(2026-10-24T19:02:15, confirmed by inspecting the last row directly) with continuous `tradeable=false`
evidence up to that point — but the real resolution needs a sample from the actual Sunday reopening,
2026-10-25T21:04:56, roughly **26 hours past the evidence window's own end**. This was independently
confirmed to be a **structural** limitation, not a bug in the retry/fetch logic: `research_gap_evidence.
reconstruct()`'s trailing-gap and `pending_nontradeable` branches resolve only from an actual tradeable
quote sample already in the evidence (a continuous run of `tradeable=false` quotes for closure-bridging,
or an ordinary post-deadline receipt) — never from `candle_proof()`. A fetch was attempted (confirmed via
instrumented run); the returned candles could not and did not change the outcome, because that code path
never consults fetched candle data at all. Reusing `research_gap_evidence.py` **unmodified** means this
category of case — evidence export cut off before the reopening, affecting v2-003 and (see below) 7 more
of the 18 originally-unresolved trades — cannot be resolved by this engine as built, regardless of how
much candle data is fetched.

**This is 1 of 4 controls failing to validate — a material discrepancy per the brief's own criterion.**
Per instruction, this is reported as a demonstrated, explained limitation; it is not tuned away, and the
aggregate performance figures below are presented with this caveat attached, not as an unqualified
conclusion.

## Previously-reconstructed trade validation (2 trades)

| Trade | Prior supplementary reconstruction (phase 1) | This engine, independently | Result |
|---|---|---|---|
| v2-001 | USDJPY long, stopped, exit 157.778 @ 2026-10-22T03:08:42.529605Z, R=−1.000000 | stopped, exit 157.778 @ 2026-10-22T03:08:42.529605Z, R=−1.000000 | **EXACT MATCH** |
| v2-002 | USDJPY short, targeted, exit 156.309 @ 2026-10-23T00:05:48.922447Z, R=+1.017280 | targeted, exit 156.309 @ 2026-10-23T00:05:48.922447Z, R=+1.017280 | **EXACT MATCH** |

Both reproduce the prior, already-published reconstruction exactly, via a fully independent code path
(different engine, different orchestration, same underlying `research_gap_evidence.py` rules). This is
expected given both reuse the identical rules module, and confirms the CSV/evidence-adapter plumbing
introduces no discrepancy versus reading the observer's own logs directly.

## Engine-level fixes made and verified during this validation (full transparency)

Two real defects were found and fixed while reproducing these controls — neither is a scoring-rule
change; both are in this engine's own orchestration (`engine.py`), confirmed via the test suite
(47/47 passing) before being trusted against real data:

1. **Timing bug**: an `as_of` fixed to an earlier captured snapshot timestamp made every freshly-fetched
   candle look like "information from the future" (its own `fetched_at_utc` is real wall-clock time,
   necessarily later), so `candle_proof()` correctly rejected every candle this engine fetched. Fixed by
   advancing (never rewinding) `now` to real time after each live fetch.
2. **Request-volume bug**: fetching only the exact disqualifying gap, one live request per gap, is far
   too slow for a real 30h+ window with many small scattered receipt gaps (consistent with
   `PHASE2_INVESTIGATION.md`'s ~1.157% attempt-failure rate). Fixed by padding each fetch (bounded by
   the per-request span cap) so nearby gaps reuse already-fetched candles — confirmed via a dedicated
   test that a second nearby gap triggers zero additional live calls.

An attempted third fix (feeding fetched candles into the trailing/closure case) was built, tested,
found **empirically ineffective** (confirmed by direct test, not just code-reading), and **removed**
rather than shipped as a false improvement — see the code comment in `engine.py` above the retry loop
and the regression test `test_trailing_gap_cannot_be_resolved_by_candles_alone`.

## Review of `research_gap_evidence.py` reuse for contract compatibility

Beyond passing synthetic tests, this was checked against the real production contract three ways:
1. **Bytecode-level verification** (done in the prior repair-branch review): `build_paper_ledger` only
   ever forwards `quotes` to `score_decision` as a bare global call — confirmed by reading the real
   `score.py`, not assumed.
2. **Exact arithmetic match on deadlines**: this adapter inverts `original_deadline_utc` into
   `max_holding_time_hours` via exact subtraction from `entry_timestamp_utc` — verified to reproduce
   `30.0` exactly for all 22 trades (no rounding drift), confirming the real export's own deadlines were
   computed the same way (`assumed_entry_time_utc + max_holding_time_hours`, never `entry_expiry_utc +
   holding duration`).
3. **Live control validation above**: the ultimate compatibility check — 3 of 4 independently
   reconstructed outcomes match the frozen scorer's own real production output exactly.

**Conclusion: proceed to Step 4/5 with the explicit caveat that closure/trailing-gap trades (8 of the
16 remaining originally-unresolved trades, including control v2-003) carry this demonstrated
limitation.** This is disclosed in every aggregate figure below, not treated as resolved.
