# Phase 3 — WP6 V3 Architecture Recommendations (architecture only — nothing implemented)

Builds on Phase 2's own section (`EXECUTIVE_SUMMARY.md` §10), updated with what Phase 3 actually proved
rather than merely recommended. Per the authorization: V3 observer development, production integration,
deployment, and retrospective modification of V2 remain unauthorised — nothing below is implemented or
proposed for implementation here.

- **Evidence-window margin is now proven, not just recommended.** Phase 2 recommended giving an automated
  recovery pipeline's bounded evidence window enough margin past the nominal deadline to catch a genuine
  closure reopening. Phase 2B's 96h-margin export directly demonstrated this: v2-009, v2-017 and v2-021 —
  3 of 22 originally-unresolved trades — resolved *only* because of the wider window, with no other change
  to rules or data. This is no longer a hypothesis.
- **A fetch orchestration layer must handle gaps wider than one historical-source request**, not just pad
  small ones. Phase 2's padding technique alone crashed on Phase 2B's wider window (a 176,760s gap); the
  fix (chunk into bounded sub-requests, with a safety margin for a source's own boundary-rounding
  behavior) is now implemented and tested in `engine.py` and should be carried into any V3 pipeline that
  reuses this fetch layer, not re-derived from scratch.
- **Any pipeline ingesting a multi-hundred-MB export must stream, never load-to-dict.** The OOM incident in
  this phase (loading 619MB/~1.9M rows at once) is a concrete, now-fixed example
  (`codex_export_adapter.spill_evidence_by_trade`) of a failure mode a V3 automated pipeline would also hit
  if it ever processes a comparably large historical export on a memory-constrained host. This is now a
  reusable, tested pattern (already used in production by `research_snapshot_publisher.py`'s own
  `_spill_bounded_quotes`), not a one-off workaround.
- **A same-provider secondary log (V1) is useful only as explicit, separately-labelled corroboration, never
  merged.** WP3's targeted, single-pass interval join (bisect-indexed, ~47s for a 6.2M-row log, no repeated
  scanning) is a reusable, tested, efficient pattern for exactly this kind of targeted cross-check and
  could be generalized into a V3 pipeline's own secondary-corroboration step — but the result in this phase
  (v2-020's weekend-closure corroboration) also shows its proper limit: it never changed an authoritative
  outcome, and shouldn't.
- **Reconstruction coverage has a real ceiling.** Even with the 96h-margin evidence, the chunked-fetch fix,
  and the targeted V1 cross-check, 12 of 22 entered trades (55%) remain genuinely unresolved under the
  original V2 contract — most for want of *any* retained observation in a specific window, not for any
  remaining engine limitation. The implication for V3 is about prevention, not better reconstruction: a
  production observer's own collector needs high enough receipt density/reliability that these gaps don't
  occur in the first place. No amount of after-the-fact historical-source recovery fully substitutes for a
  collector that didn't miss the window.
- **The standalone engine remains directly reusable** (unchanged from Phase 2's assessment): the frozen
  scoring rules, the pluggable historical-source interface, and the CSV/export adapter stay cleanly
  separated; only the adapter layer would need rebuilding against live logs instead of a CSV export for a
  real-time V3 pipeline.

## Supplement: explicit assessment against the original 9 WP6 points

The original authorization asked this section to assess 9 specific points. The write-up above directly
covers 4 of them (reuse of the standalone engine; automatic historical gap recovery; evidence export
margins; V1 as a secondary collection process). The remaining 5 are addressed here, each confirmed from a
concrete finding in this phase rather than asserted abstractly.

1. **Reuse of the standalone reconstruction engine — addressed above.** Essential to keep if any
   automated recovery is built at all (it already cleanly separates frozen scoring rules, a pluggable
   source interface, and the export adapter); optional in the sense that V3 doesn't strictly require
   automated recovery to exist.
2. **Automatic historical gap recovery — addressed above.** Optional enhancement, not essential: it only
   ever helps the subset of gaps where *some* retained observation exists on either side; it cannot help
   the majority failure mode found in this phase (no retained observation at all).
3. **Appropriate evidence export margins — addressed above, now proven not just recommended.** Essential
   if any bounded-window export is used at all (an under-margined window silently misses genuine closure
   reopenings — this is exactly what Phase 2A's narrower window did).
4. **Affirmative market closure evidence — essential, and the root cause of most remaining gaps.**
   `research_gap_evidence.reconstruct()`'s own `_closure()` check requires a continuous run of *explicit*
   `tradeable=false` receipts spanning a gap — silence (no receipts at all) is never treated as implicit
   closure, correctly, because silence is indistinguishable from a collector outage. Most of the 12
   trades still unresolved in this phase fail for exactly this reason: an absence of receipts, not a
   disputed price. **V3's collector must keep emitting explicit tradeable=false heartbeat receipts
   through a closure, never go silent and rely on a gap being interpreted as closure after the fact.**
   This is the single highest-leverage fix available to V3 — it directly addresses more of the 12
   remaining-unresolved trades than any historical-recovery technique in this phase did.
5. **Receipt-time versus provider-time continuity — essential, and concretely demonstrated by v2-020 in
   WP3.** v2-020's 49 "gaps" showed a steady `received_at_utc` cadence (the collector was polling
   throughout) while the underlying `oanda_time_utc` provider timestamp was frozen at the Friday close for
   the entire Saturday closure. Receipt-time continuity (the collector is alive and polling) and
   provider-time continuity (the price itself is fresh, not a cached last-known value) are **different
   properties and must be tracked separately.** A collector that only logs receipt timestamps, or that
   treats a steady polling cadence as proof of fresh data, cannot distinguish "market closed, correctly
   serving last price" from "market open, feed stuck serving stale data" — the latter is a real outage
   that matters for live trading risk, not just reconstruction.
6. **V1 as a secondary collection process — addressed above.** Confirmed optional/supplementary only
   (same OANDA account as V2, not provider-independent); useful for corroboration, never for an
   authoritative outcome.
7. **OANDA authorisation failure handling — essential, found as a genuine gap in this phase, not
   theoretical.** `historical_sources.py` raises a plain `RuntimeError` for both a wrong environment and
   missing/invalid credentials (confirmed at `historical_sources.py:62` and `:88`); `engine.py`'s fetch
   loop catches it with the same broad `except Exception` used for every other fetch failure, so an
   **authorization/credential failure is currently indistinguishable from genuine insufficient evidence**
   — both land in `unresolved_insufficient_historical_data`. This happened for real in this phase (a
   credentials-sourcing mistake during a Phase 2B rerun produced exactly this misleading classification,
   caught only by inspecting the `reason` string manually). **V3 must classify an auth/environment
   failure as its own distinct, alertable state**, never silently folded into "insufficient historical
   data" — the current behavior could mask a real OANDA outage as if it were a legitimate, permanent data
   gap.
8. **Whether a streaming collector remains justified — yes, essential, and this phase is direct evidence
   for it, not against it.** Even after the widest practical evidence-export margin, both engine bugs
   fixed, and a full independent V1 cross-check, 12 of 22 entered trades (55%) remain genuinely
   unresolved — almost entirely because no observation was ever retained in the gap window to begin with.
   No amount of after-the-fact historical reconstruction can substitute for a collector that didn't miss
   the window. This phase argues *for* continued and probably increased investment in collector
   reliability (denser polling, redundancy, the affirmative-closure-heartbeat fix in point 4), not for
   deprioritizing it in favor of reconstruction tooling.
9. **Whether an independent commercial data provider remains necessary — no, not for the core problem,
   and this is an optional enhancement at most.** OANDA's own S5 historical candles were sufficient for
   every gap in this phase that was recoverable at all (this phase's successful recoveries used OANDA
   practice candles exclusively, never a third-party feed). The majority failure mode — no retained
   observation in the gap window — is a **tradeability** question (would this specific account have been
   able to execute at that moment), which is inherently broker/account-specific; a third-party market-data
   vendor can supply an independent price series but cannot answer that question any more authoritatively
   than OANDA's own historical API already does. A commercial provider could still be worth evaluating
   purely for redundancy against an OANDA-side outage (see `HISTORICAL_DATA_SOURCE_ASSESSMENT.md`'s
   already-assessed-but-not-wired candidates), but it would not have resolved any of the 12 trades that
   remain unresolved in this phase, so it is not load-bearing for V3's core recovery capability.

### Essential vs. optional, summarized

**Essential:** affirmative closure heartbeats from the collector (4); tracking receipt-time and
provider-time continuity separately (5); distinct, alertable handling of auth/credential failures (7);
continuing to invest in the streaming collector itself (8); adequate evidence export margins if any
bounded export is used (3).

**Optional enhancements:** reusing the standalone engine's architecture (1); automated historical gap
recovery (2); V1 same-provider corroboration (6); an independent commercial data provider (9).

## Note on this authorization's own text

Section 9 of the Phase 3 authorization (this section) was received truncated mid-sentence ("9. WP6 — V3
architecture recommendations\nBase...") — the brief's own guidance for this section was never fully
received. This document was written from Phase 2's existing V3 section plus Phase 3's actual findings
rather than from that missing text; if the brief intended something more specific here, it should be
resupplied.