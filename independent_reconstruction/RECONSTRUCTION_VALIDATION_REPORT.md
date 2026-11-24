# Reconstruction Validation Report — STATUS: pending real data

This report cannot be completed yet. The brief's section 7 validation controls (replaying the 4
live-completed trades and the 2 previously-reconstructed trades, comparing exit classification/price/
timestamp/R against the originals, with tolerances fixed in advance — see
`RECONSTRUCTION_METHODOLOGY.md` section 5) require the real `V2_RECONSTRUCTION_INPUT.csv` and
`V2_OBSERVATION_EVIDENCE.csv`. Neither has arrived in `codex_audit_exports/` as of 2026-11-24.

Producing numbers here now would mean inventing trade data to validate against — exactly the kind of
fabrication this whole exercise exists to avoid. This document will be replaced with real findings as
soon as the inputs exist and the engine (already built and tested — 20/20 synthetic tests passing, see
`tests/test_engine.py`) has been run against them.

## What will go here once the inputs arrive

1. **Per-control-trade comparison table**: for each of the 4 live-completed trades and 2
   previously-reconstructed trades — original classification/exit price/exit timestamp/R vs. this
   engine's independently reconstructed values, and whether each falls inside the pre-registered
   tolerance (exact match to 5 decimals on price, exact to the second on timestamp, `1e-9` relative
   tolerance on R).
2. **Any discrepancy, investigated and explained — not tuned away.** If this engine disagrees with a
   live-observed result, the brief requires reporting why, not adjusting the rules until it agrees.
3. **A comparison against the two previously-reconstructed trades' own methodology** (the
   `supplementary_gap_evidence_v1` audit already shipped — see `BOUNDED_RESEARCH_REPAIR.md`) — since
   this engine deliberately reuses those same rules verbatim, agreement here is close to definitional;
   a disagreement would indicate a bug in this engine's CSV/orchestration layer, not in the shared rules
   module, and would be investigated as such.
4. **Overall reliability assessment** of the 16 originally-unresolved trades' reconstruction, informed
   by how many of the 6 controls validate cleanly.

## Reproduction (once inputs exist)

```bash
cd independent_reconstruction
../.venv/bin/python engine.py \
  --input ../codex_audit_exports/V2_RECONSTRUCTION_INPUT.csv \
  --evidence ../codex_audit_exports/V2_OBSERVATION_EVIDENCE.csv \
  --out-dir output --as-of <fixed UTC timestamp> --fetch-historical-candles
```
