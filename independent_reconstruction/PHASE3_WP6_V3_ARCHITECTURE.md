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

## Note on this authorization's own text

Section 9 of the Phase 3 authorization (this section) was received truncated mid-sentence ("9. WP6 — V3
architecture recommendations\nBase...") — the brief's own guidance for this section was never fully
received. This document was written from Phase 2's existing V3 section plus Phase 3's actual findings
rather than from that missing text; if the brief intended something more specific here, it should be
resupplied.