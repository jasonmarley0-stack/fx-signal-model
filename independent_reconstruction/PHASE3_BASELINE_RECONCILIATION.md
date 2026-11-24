# Phase 3 — File Integrity and Baseline Reconciliation

## File integrity

| File | Manifest hash | Actual hash | Result |
|---|---|---|---|
| `PHASE2B_EVIDENCE_EXTRACTION_REPORT.md` | `89ab0311...` | `89ab0311...` | Match |
| `V2_EVIDENCE_WINDOW_AUDIT.csv` | `1fba1f38...` | `1fba1f38...` | Match |
| `V2_EXTENDED_OBSERVATION_EVIDENCE.csv` | `2a2ec5eb...` | `2a2ec5eb...` | Match (619,321,495 bytes, verified with `shasum`) |
| `V2_REMAINING_GAP_REGISTER.csv` | `00ba87cb...` | `92de9f7a...` | **MISMATCH** |
| `generate_phase2b.py` | `41c56eb0...` | `41c56eb0...` | Match |

**The `V2_REMAINING_GAP_REGISTER.csv` mismatch was investigated, not ignored.** Re-verified with three
independent tools (`shasum`, `openssl dgst`, Python `hashlib`) — all three agree with each other and
disagree with the manifest, ruling out a tool error on this end. The file's actual byte count
(148,975) matches the manifest's own claimed byte count exactly, and its content is well-formed and
independently consistent with already-known facts (e.g. its row for v2-001's first gap —
`2026-10-21T10:07:05.325371Z`→`2026-10-21T10:07:20.778108Z`, 15.45s — matches Phase 2's own
forensic diagnosis of that exact gap exactly). This is most consistent with a **manifest transcription
error**, not file corruption or tampering, and the file was used on that basis — but this is reported
explicitly, not silently resolved. 4 of 5 files verified exactly; the 22-row-mismatch tally in
`V2_EVIDENCE_WINDOW_AUDIT.csv`/`V2_REMAINING_GAP_REGISTER.csv` content was additionally spot-checked
against independently-rederived values below.

**Provenance note (also independently checked, not just read):** the extraction report states the
evidence is sourced "only from the original V2 collector log," run `f5c9461afc3f446195e64ecdae321281` —
confirmed by inspecting the extended CSV's own rows directly (`run_id` column, `source_type=
v2_original_live_collector` on every row sampled).

## Path correction

The brief stated the files were already copied into `independent_reconstruction/input/phase2b/`. They
were not — they were found at the external audit directory
(`/Users/jasonmarley/Documents/ChatGPT/Signal IQ - FX/audit/2026-11-24-v2-observer-phase2b/`), the same
location pattern as Phase 2's own export. Imported from there, read-only, following the identical
procedure used in Phase 2; originals re-verified byte-unchanged afterward.

## Baseline reconciliation against the Phase 2 ledger

Confirmed against `independent_reconstruction/output/V2_RECONSTRUCTED_TRADE_LEDGER.csv` (Phase 2):

- 22 entered trades, 4 live-completed (v2-003, v2-008, v2-015, v2-016), 2 previously-reconstructed
  (v2-001, v2-002, already validated), 1 newly reconstructed (v2-014), 7 total determinate outcomes,
  15 remaining unresolved, combined known return **+1.1787R** — all match the stated baseline exactly.
  No discrepancy.

## Reconciling "7 potentially recoverable" vs. the previously-reported "8 structural/trailing cases"

Phase 2's 8 structural/trailing-category trades: v2-003, 005, 006, 007, 009, 017, 021, 022.
Phase 2B's "7 potentially recoverable" list: v2-003, 005, 006, 007, 009, 017, 021.

**These are not in conflict — the 7 is a subset of the 8, with the 8th (v2-022) explicitly excluded
with a stated, independently-verified reason** (see WP2 below). There is no double-count risk: v2-003
is correctly flagged by the brief itself as *already a live-completed validation control*, not an
additional recovery candidate, and is treated that way throughout this phase (evaluated under WP1, not
WP2).

## Trade IDs checked for double-counting

Verified no trade ID appears in more than one role across Phase 2 + Phase 2B: the 4 live controls, the
2 prior reconstructions, the 1 new recovery, and the 15 unresolved (7 structural candidates + 1
confirmed-unrecoverable + 7 mid-scan-gap cases) partition the 22 entered trades with no overlap and no
omission. (4+2+1+15 = 22.)

## WP1 — Live control validation (against the extended evidence)

**Engine bug found and fixed first.** The first reconstruction attempt against the extended (96h-margin)
evidence crashed on 3 of the 6 WP2 candidates with "Requested span 176760s exceeds the 24950s per-request
bound." Root cause: `engine.py`'s padding logic (the Phase 2 fix) only ever shrinks padding toward 0 as a
gap widens — it never splits a request when the *raw* gap itself already exceeds
`OandaPracticeSource.MAX_SPAN_SECONDS` (24,950s). The Phase 2A evidence window (~30h max) never produced a
gap this wide; the Phase 2B 96h-margin export did. Fixed in `engine.py` by chunking any oversized fetch
into consecutive bounded sub-requests. A second, subtler issue surfaced immediately after: a chunk sized to
land exactly on 24,950s was still rejected (24,955s) because `OandaPracticeSource.fetch()` snaps request
boundaries outward to the nearest 5s grid before checking its own bound — fixed with a 10s safety margin
applied everywhere `MAX_SPAN_SECONDS` is used. Both fixes are regression-tested
(`test_oversized_gap_is_split_into_bounded_fetches_not_widened_past_the_cap`, constructed to reproduce the
exact 24,950→24,955s rounding failure); all 52 local tests pass. Neither fix touches
`research_gap_evidence.py` (the frozen scoring core) — both are purely in the reconstruction engine's own
fetch orchestration, the same category as the two bugs fixed in Phase 2.

**v2-003 (live control, time_exited):** reconstructed exit price **1.40487**, exit time
**2026-10-25T21:04:56.913807+00:00**, R **-0.7541152263374584** — exact match (to the microsecond and to
full float precision) against the original live-observed KPI snapshot. Classification, price, timestamp
and R all agree.

**v2-008, v2-015, v2-016 (live controls, targeted):** all three also reproduce their original
exit_price/exit_time_utc/r_multiple exactly against the live KPI snapshot.

**v2-001, v2-002 (previously reconstructed, Phase 2A) and v2-014 (Phase 2's new recovery):** all three
reproduce Phase 2A's own already-established exit_price/exit_time_utc/r_multiple **exactly**, byte-for-byte,
against the extended evidence. This confirms the evidence-window extension and the engine fixes above
changed nothing about previously-settled outcomes — scoring logic is unchanged.

**No material discrepancies found.** All 7 controls/prior-reconstructions pass with exact agreement.

## WP2 — Supplementary structural recovery: results and a discrepancy with Codex's claim

Reran all 6 candidates against the extended evidence with the fixed engine:

| Trade | Result | Exit | Time (UTC) | R |
|---|---|---|---|---|
| v2-009 | **reconstructed_time_exit** | 0.81707 | 2026-11-04T12:03:59.074366 | +0.0967391304 |
| v2-017 | **reconstructed_time_exit** | 0.55495 | 2026-11-12T16:04:03.191781 | +0.2220828105 |
| v2-021 | **reconstructed_time_exit** | 0.54899 | 2026-11-18T20:03:55.896440 | +0.0031625553 |
| v2-005 | unresolved_insufficient_historical_data | — | — | — |
| v2-006 | unresolved_insufficient_historical_data | — | — | — |
| v2-007 | unresolved_insufficient_historical_data | — | — | — |

**3 of 6 genuinely resolve. v2-005/006/007 do not — this contradicts Codex's "potentially recoverable"
claim, and the discrepancy has been traced to a specific, real cause, not an engine limitation:**

With both fetch bugs fixed, reconstruction for v2-005/006/007 no longer fails on a fetch error — it reaches
`research_gap_evidence.reconstruct()`'s own (unmodified) refusal: *"Gap lacks sufficient non-touch or
non-tradeability evidence."* Reading that function directly (`research_gap_evidence.py:141-158`): for every
gap between tradeable quotes wider than `GAP_SECONDS`, it first checks for **continuous explicit
non-tradeability** across the *exact* gap (a run of `tradeable=false` receipts) and only falls back to
candle-envelope proof if that's absent; if candle proof also fails, the whole trade is immediately refused
— it does not proceed to any other gap.

Codex's "potentially recoverable" assessment cited `V2_EVIDENCE_WINDOW_AUDIT.csv`'s
`closure_bridge_continuous_at_15s=True` for v2-005/006/007 — but that column measures continuity across the
**weekend/holiday market-closure window specifically**, not every gap in the trade's lifetime. The gap this
engine actually gets stuck on for these three trades is a separate, ~50,000-second span. Because
`_closure()` requires a continuous run of received `tradeable=false` quotes, and because fetched OANDA S5
candles for that specific span still could not establish a conservative no-touch envelope, the refusal is
genuine: either that window had no retained receipts at all (a real collector gap, not a market closure —
`_closure()` can't bridge an absence of samples), or price action in the real candle history came close
enough to stop/target that the deliberately conservative `candle_proof()` correctly declines to rule out a
touch. Both are legitimate, contract-compliant outcomes, not a bug.

**Conclusion: Codex's closure-bridge-continuity metric is necessary but not sufficient for recoverability.**
v2-005/006/007 have a second, separate unresolved gap that the closure-bridge metric alone does not
capture. This is reported as a finding rather than silently reconciled — do not relax the
continuous-non-tradeability requirement to force a match with Codex's figure, per the authorization's own
instruction.

**v2-022:** independently reconfirmed unresolved by the actual engine run (not just by the prior
documentary analysis), consistent with the previously-verified 6,060.559s discontinuity.

## WP3 — Targeted V1 evidence recovery

Ran `run_v1_targeted_recovery.py` on the droplet (V1's `quotes_log.jsonl` lives there only, 1.47GB /
6,173,132 lines). Single sequential pass, never reopened or rescanned: **elapsed 46.98s, 6,173,132 rows
scanned, 2,645,628 matched a target pair.** No per-interval or per-trade re-scanning.

**A second file defect found and worked around, not silently dropped.** The register has one genuinely
malformed line (line 493: 7 fields instead of 15, `['me-provider log exists', '', '', '', '', 'false
(archive ends 2026-10-20 before all entries)', 'V2 gap is immutable; ...']`) — a corrupted, partial
duplicate of the complete row at line 543. Traced by direct inspection, not guessed at; the script now
skips any row whose field count doesn't match the header (1 row skipped, logged) rather than crashing or
silently misreading it.

**v2-022's duplicate-row conflict (flagged in the file-integrity section above) is now resolved by direct
evidence, not inference.** The register has two real, conflicting rows for `v2-022` gap 1: line 492 says
"0 observations, No V1 observation retained in gap"; line 543 says "not extracted; retained same-provider
log exists." The independent full-log scan of the exact gap interval
(2026-11-20T22:45:32.977058Z–2026-11-21T00:03:47.483426Z, GBPUSD) found **zero** V1 observations —
confirming line 492 was correct and line 543's "not extracted" label was simply wrong (there was never
anything to extract). This is now the third independent line of evidence that v2-022 is genuinely
unrecoverable (alongside the window-audit's own 6,060.559s discontinuity figure and the bug-fixed engine's
own refusal against real data).

**Trustworthiness spot-check.** 2 of the 4 planned verification-sample keys existed in the register as
already-extracted rows (v2-003 and v2-008 don't have a logged gap 1); for both that did
(v2-001#1, v2-015#1), the independently-found observation (timestamp, bid, ask, tradeable) matches Codex's
already-reported values exactly. Codex's own V1 extraction, where it was done, checks out.

**v2-020's 49 "not extracted" mid-scan gaps: all 49 resolved to a V1 observation, but all 49 share the
identical `oanda_time_utc` (2026-11-13T21:59:05.233380025Z) and `tradeable=false`, despite
`received_at_utc` spanning nearly 11 hours (2026-11-14 12:56–23:47).** This is not a stale/broken feed: 
2026-11-13 is a Friday (21:59:05 UTC matches the standard FX week close) and 2026-11-14 is a Saturday —
the frozen last-traded price with `tradeable=false` is the correct, expected signal for a weekend market
closure on the same OANDA account V2 itself uses. This is **same-provider corroboration, not independent
V2 evidence** — per the authorization, it is not merged into V2's own evidence and does not change
v2-020's authoritative classification (still `unresolved_insufficient_historical_data`). It is reported
here, separately, as an exploratory observation only: V2's 49 missing receipts during this exact window
coincide with a period when the same account's parallel feed shows continuous closure, which is
*consistent with* (but does not prove, since it is not V2's own receipt stream) v2-020 being
closure-bridgeable if V2's own evidence ever surfaces. No outcome for v2-020 is changed or claimed on this
basis.

**No V1 observation was silently merged into any V2 evidence file or authoritative result.**
