# Signal IQ — Independent Historical Reconstruction: Phase 2 Executive Summary

Isolated branch `research/v2-independent-reconstruction`. No production code, data, or dashboard
classifications modified. Not merged, not deployed. All source data (Codex's real export, the
production evidence files, this engine's own OANDA fetch cache) preserved under
`independent_reconstruction/`. **Stopping here for independent review, per the brief.**

## 1. Input validation results

All 22 entered trades in `V2_RECONSTRUCTION_INPUT.csv` loaded cleanly against this engine's schema
adapter: **zero validation errors** (every required field present, every stop/entry/target geometry
consistent with its direction, every deadline inversion exact). Cross-checked against the authoritative
captured KPI snapshot (`source_research_kpi_v2.json`) by pair + exact entry timestamp: **all 22 matched,
zero "not found", zero state mismatches.** The apparent "19 vs 18 unresolved" discrepancy in the
snapshot's own internal counter is fully explained, not a real discrepancy: the dashboard's
`unknown_total=19` counts 18 entered-but-unresolved trades **plus** 1 never-entered
`insufficient_data_entry` row that isn't a trade at all; the brief's "18 live-unresolved trades" baseline
correctly excludes that row. Full reconciliation: 28 ledger rows = 23 eligible + 5 suppressed; 23 eligible
= 22 entered + 1 insufficient-data miss; 22 entered = 4 live-completed + 18 live-unresolved (2 of which
were already supplementarily reconstructed in a prior session). **No discrepancies found.**

## 2. Live control validation results

**3 of 4 live-completed controls validated exactly** (exit price, exit timestamp, and R all identical
to the production original — v2-008, v2-015, v2-016). **1 of 4 (v2-003) did not validate**: its real
resolution needs a quote ~26h past the evidence export's own per-trade window cutoff, and
`research_gap_evidence.py`'s trailing/closure-reopening logic resolves only from an actual tradeable
quote sample — never from fetched candle data, confirmed by direct test, not just by reading the code.
This is a demonstrated limitation of reusing the existing rules module as-is against this specific
evidence export, not a scoring bug. Full detail, including two real orchestration bugs found and fixed
during this validation (a candle-rejection timing bug and a request-volume efficiency fix), in
`RECONSTRUCTION_VALIDATION_REPORT.md`.

## 3. Previously-reconstructed trade validation results

**Both validated exactly** (v2-001 stopped −1.000R, v2-002 targeted +1.0173R), reproducing the prior
session's published supplementary reconstruction byte-for-byte via a fully independent code path.

## 4. Number of successfully recovered outcomes

**1 genuinely new outcome recovered**: v2-014 (GBPUSD long, stopped, R=−1.0052). v2-001 and v2-002 are
**not new** — they reproduce an already-known, already-published prior reconstruction, serving as
validation rather than fresh recovery.

## 5. Number of remaining unresolved outcomes

**15 of 22 entered trades.** Of these, 8 fall into the *same category* v2-003 (a live control) failed
to validate — a trailing/closure-reopening gap this engine cannot resolve by design, not a
candle-availability problem. The other 7 hit a genuine mid-scan evidence gap where a candle was fetched
but still failed to prove no-touch.

## 6. Complete trade-by-trade outcome table

Full table (entry, stop, target, deadline, reconstructed exit, classification, R, evidence source,
cross-check, limitations) for all 22 trades: **`independent_reconstruction/output/
V2_RECONSTRUCTED_TRADE_LEDGER.csv`**. Condensed:

| Trade | Pair/Dir | Original | Result | R |
|---|---|---|---|---|
| v2-001 | USDJPY long | incomplete_coverage | reconstructed: stopped | −1.0000 |
| v2-002 | USDJPY short | incomplete_coverage | reconstructed: targeted | +1.0173 |
| v2-003 | USDCAD short | time_exited (live) | **validation gap** — unresolved by this engine | (live: −0.7541) |
| v2-004 | EURUSD short | incomplete_coverage | unresolved — insufficient historical data | — |
| v2-005 | GBPUSD long | incomplete_coverage | unresolved — trailing/closure gap | — |
| v2-006 | USDJPY short | incomplete_coverage | unresolved — trailing/closure gap | — |
| v2-007 | USDCAD short | incomplete_coverage | unresolved — trailing/closure gap | — |
| v2-008 | USDJPY long | targeted (live) | **validated exactly** | +1.0725 |
| v2-009 | USDCHF short | incomplete_coverage | unresolved — trailing/closure gap | — |
| v2-010 | EURUSD long | incomplete_coverage | unresolved — insufficient historical data | — |
| v2-011 | USDJPY long | incomplete_coverage | unresolved — insufficient historical data | — |
| v2-012 | USDCHF short | incomplete_coverage | unresolved — insufficient historical data | — |
| v2-013 | USDCAD short | incomplete_coverage | unresolved — insufficient historical data | — |
| v2-014 | GBPUSD long | incomplete_coverage | **reconstructed: stopped (new)** | −1.0052 |
| v2-015 | EURUSD short | targeted (live) | **validated exactly** | +0.8926 |
| v2-016 | USDJPY long | targeted (live) | **validated exactly** | +0.9556 |
| v2-017 | NZDUSD short | incomplete_coverage | unresolved — trailing/closure gap | — |
| v2-018 | NZDUSD short | incomplete_coverage | unresolved — insufficient historical data | — |
| v2-019 | AUDUSD short | incomplete_coverage | unresolved — insufficient historical data | — |
| v2-020 | GBPUSD short | incomplete_coverage | unresolved — insufficient historical data | — |
| v2-021 | NZDUSD short | incomplete_coverage | unresolved — trailing/closure gap | — |
| v2-022 | GBPUSD short | incomplete_coverage | unresolved — trailing/closure gap | — |

## 7. Aggregate performance statistics

See `V2_PERFORMANCE_RECOVERY_REPORT.md` for full detail, by-pair/direction breakdowns and the explicit
"not meaningful at this n" caveats. Headline (n is small throughout — descriptive only):

- **A. Live-observed (n=4)**: win rate 75%, total R +2.167, avg R +0.542, profit factor 3.87, max DD 0.754R.
- **B. Independently reconstructed, new-only (n=3)**: win rate 33%, total R −0.988, avg R −0.329, profit factor 0.51, max DD 1.005R.
- **C. Combined, mixed-provenance (n=7 determined / 15 unresolved)**: win rate 57%, total R +1.179, avg R +0.168, expectancy +0.168R/trade, profit factor 1.43, max DD 1.005R.

## 8. Transaction-cost sensitivity

At 0/0.02/0.05/0.10 R assumed cost per trade (View C, n=7 resolved): total R +1.179 → +1.039 → +0.829 →
+0.479. Stays positive through all four assumptions shown, but n=7 means this says little about the
real underlying cost sensitivity.

## 9. Reconstruction limitations

- **Structural**: 8 of 15 unresolved trades (plus control v2-003) fail specifically because
  `V2_OBSERVATION_EVIDENCE.csv`'s per-trade window ends at the nominal deadline with no closure margin,
  and `research_gap_evidence.py`'s trailing/closure logic needs an actual quote, not a candle, to
  resolve that case. **Extending the evidence export's window would likely resolve several of these
  without any engine change.**
- **Genuine evidence gaps**: 7 unresolved trades hit a real mid-scan gap where a candle was fetched but
  still didn't prove no-touch (confirmed for v2-004/EURUSD in a prior session's forensic diagnosis: a
  single ~3-second OANDA boundary-candle absence, not a level touch).
- **V1 is not an independent data source**: verified directly (MD5 of all three `oanda.env` files on
  the droplet are byte-identical) — v1 and v2 share the same OANDA account/credentials/practice
  environment, polled by separately-scheduled but non-independent processes. v1's `quotes_log.jsonl`
  is a legitimate secondary cross-check (different poll timing can show a sample where v2's own
  collector had a gap) but must never be described as independent-provider corroboration.
- **Costs genuinely unmeasured**: financing, commission, and real execution slippage beyond the sampled
  bid/ask are not observed anywhere in this exercise.
- **Sample size**: every aggregate figure in section 7 is descriptive; none should be read as evidence
  of the strategy's real edge.

## 10. Recommended implications for V3 development (architecture only — nothing implemented)

Building on `PHASE2_INVESTIGATION.md`'s own architecture assessment (option D: "primary feed + historical
backfill... supplementary evidence only, never silently converts live observation" — the same principle
this whole exercise followed):

- **OANDA S5 historical data is viable as an automated gap-recovery input**, now demonstrated against a
  real 4-week, 7-pair production window, not just synthetic data: 25 live requests resolved what they
  could resolve, well within rate limits, fully cacheable/reproducible. The padded-fetch technique
  (reuse a wider already-fetched response for nearby small gaps) is a reusable, tested optimization
  worth carrying into any automated pipeline — most receipt gaps in real data are small and clustered,
  not evenly spread.
- **v1's quote log is a legitimate low-cost secondary signal, never a primary one** — see the
  provider-independence finding above. An automated pipeline should label it explicitly as such.
- **The standalone engine (`engine.py` + `historical_sources.py` + `codex_export_adapter.py`) is
  directly reusable**: it already cleanly separates (a) the frozen scoring rules [untouched], (b) a
  pluggable historical-source interface [OANDA implemented; Dukascopy assessed-but-not-wired, see
  `HISTORICAL_DATA_SOURCE_ASSESSMENT.md`], and (c) CSV/export-format adaptation. A V3 automated
  pipeline could reuse (a) and (b) directly; only (c) would need rebuilding against live logs instead
  of a CSV export.
- **The one real limitation that matters for V3**: any automated recovery pipeline must give its
  bounded evidence window enough margin past the nominal deadline to include a genuine weekly-closure
  reopening (the production publisher's own bounded windowing already does this correctly, per
  `BOUNDED_RESEARCH_REPAIR.md` — it's specifically this *export's* narrower window that lacks it, not
  a universal problem).
- **Cost/dependency note**: real cost was near-zero (OANDA practice, already-credentialed, 25 requests);
  the dependency is entirely on the production observer continuing to log receipt timestamps/tradeable
  flags at the current fidelity.
- Per the brief: **this is a recommendation only; no V3 production change is implemented or proposed
  for implementation here.**

## Reproduction

```bash
cd independent_reconstruction
../.venv/bin/python3 -m pytest tests/ -q   # 47 passed
python3 compute_performance.py             # performance views
# Full reconstruction (uses the committed cache/, no network needed):
# run_full_reconstruction.py was executed on the droplet (OANDA credentials
# live there only); its exact output is output/full_reconstruction_results.json
```
