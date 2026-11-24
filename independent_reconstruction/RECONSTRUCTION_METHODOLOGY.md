# Independent V2 Trade Reconstruction — Methodology

**Status:** architecture, engine and tests built and passing against synthetic fixtures.
**Not yet run against real data** — `V2_RECONSTRUCTION_INPUT.csv`, `V2_OBSERVATION_EVIDENCE.csv` and
`EVIDENCE_MANIFEST.md` (Codex's deliverables) have not arrived in `codex_audit_exports/` as of this
writing. Per the brief, the final reconstruction exercise depends on receiving them; this document and
the engine are ready for that moment, not a substitute for it.

## 1. Relationship to the existing supplementary-evidence audit

This is a **new, standalone, CSV-driven** tool — not a copy of `research_gap_evidence.py` /
`research_evidence_audit.py` (which remain the production-adjacent, log-reading tools behind
`/research/ledger_v2`). It deliberately **reuses the same proven decision rules** from that earlier,
already-tested work (same side convention, same closure-bridging rule, same conservative "insufficient
evidence → unknown" discipline), because those rules were already validated against the real frozen V2
scorer and real production data. Re-deriving different rules from scratch here would risk silently
disagreeing with a result that's already correct. What's genuinely new:

- Input is an **immutable CSV export**, not live observer logs — this tool never touches the observer,
  never requires droplet access to run its core logic, and is fully reproducible by anyone with the CSVs.
- It must classify **every** trade, including ones the original audit never saw (the full 22, not just 3).
- It must independently validate against the 4 live-completed trades and the 2 previously-reconstructed
  trades as controls (section 7 below) — the earlier tool never had to do this.
- It must produce the broader performance-analysis deliverables (section 8 of the brief).

## 2. Input contract

Real files have not arrived yet. The schemas below are what this engine requires and validates against;
a file that doesn't match is reported as a measurement-contract-incompatibility error for *that file*,
not silently reinterpreted. Field names deliberately mirror the real V2 ledger/quote shapes already in
production use throughout this project, since that is the most likely shape of the authoritative export.

### `V2_RECONSTRUCTION_INPUT.csv` — one row per entered decision

| Column | Type | Notes |
|---|---|---|
| `decision_id` | string, required, unique | Falls back to `actual_recording_time_utc` only if truly absent from the source; a present-but-blank value is an error, not a fallback trigger. |
| `pair` | string, required | One of the 7 scoped pairs. |
| `direction` | `long` \| `short`, required | |
| `assumed_entry_time_utc` | ISO-8601, tz-aware, required | |
| `assumed_entry_price` | float, required, finite, > 0 | |
| `stop` | float, required, finite, > 0 | |
| `target` | float, required, finite, > 0 | |
| `max_holding_time_hours` | float, required, > 0 | Holding deadline = `assumed_entry_time_utc + max_holding_time_hours` — **never** `entry_expiry_utc + max_holding_time_hours`. |
| `original_state` | string, required | The production classification (`stopped`, `targeted`, `time_exited`, `incomplete_coverage`, etc.) — preserved, never overwritten. |
| `original_exit_time_utc` | ISO-8601 or empty | Required if `original_state` is resolved. |
| `original_exit_price` | float or empty | Required if `original_state` is resolved. |
| `original_r_multiple` | float or empty | Required if `original_state` is resolved. |

### `V2_OBSERVATION_EVIDENCE.csv` — one row per raw quote sample already collected by production

| Column | Type | Notes |
|---|---|---|
| `pair` | string, required | |
| `received_at_utc` | ISO-8601, tz-aware, required | |
| `oanda_time_utc` | ISO-8601, tz-aware, required | Provider price-creation time; must not be after `received_at_utc`. |
| `bid` | float, required, finite, > 0 | |
| `ask` | float, required, finite, >= `bid` | |
| `tradeable` | bool, required | |

Rows outside any entered trade's own window are harmless and ignored; rows inside a window with a
malformed field are treated as **absent**, not as a zero/default value (a corrupt sample must never be
read as evidence of anything).

### `EVIDENCE_MANIFEST.md`

Expected to document: the export's own provenance (run_id/source_hash of the production observer it was
pulled from, generation timestamp, row counts per file, any known exclusions). The engine does not parse
this file programmatically — it's a human-readable manifest the methodology and validation report will
quote from once received, matching it against the CSVs' own content for consistency.

## 3. Reconstruction algorithm

Reuses, verbatim, the rules already proven in `research_gap_evidence.py`:

1. **Anchor to the original entry.** Never re-derive `assumed_entry_time_utc`/`assumed_entry_price` —
   they come from the immutable input row. A trade whose original state is already resolved
   (`stopped`/`targeted`/`time_exited`) is reconstructed anyway (for the validation controls in section
   7) but is **never** allowed to override the original in the final ledger — original always wins when
   one already exists.
2. **Side convention**, unchanged: bid for long exits, ask for short exits, on both the observation
   evidence and any newly fetched OANDA candles.
3. **Deadline** = `assumed_entry_time_utc + max_holding_time_hours`, computed once, never re-derived
   from `entry_expiry_utc`.
4. **Walk chronologically** from entry through quotes matching the pair, using only
   structurally-valid samples (finite, positive, non-crossed bid/ask; parseable, non-future-dated
   provider timestamp).
5. **A gap over 15 seconds is disqualifying unless bridged by:**
   - continuous, explicit `tradeable=false` evidence spanning the *entire* gap (any weekday — this
     already-shipped rule recognizes daily rollover pauses, not only the Friday–Sunday weekly closure); or
   - a complete historical OANDA S5 candle envelope (fetched only for gaps the evidence CSV itself
     cannot already explain) whose high/low, across the *entire* gap, excludes both stop and target.
6. **Stop/target precedence always wins** over a scheduled deadline exit, including at a closure
   reopening — the first sample (or proven candle envelope) that would have triggered an exit takes
   priority over treating a later sample as "just" the deadline.
7. **No fabrication, ever:**
   - A missing bar anywhere in a gap leaves it `Unresolved — insufficient historical data` — never
     filled by interpolation or by substituting a stale value.
   - If both stop and target fall within one sample's envelope (same candle, or the same discrete quote
     sample), the ordering is **not** guessed — `Unresolved — ambiguous price sequence`.
   - Missing/invalid original parameters (`stop`/`target`/geometry inconsistent with `direction`,
     non-finite entry price, etc.) are never defaulted — `Unresolved — missing original trade parameters`.
   - A CSV that fails schema validation for a given file is `Unresolved — measurement contract
     incompatibility` for every row that depended on it, with the specific validation failure recorded.
8. **Determinism.** The engine takes no wall-clock input except an explicit `as_of` parameter (passed
   in, defaulted to a fixed, documented value for a given run — never `datetime.now()` read implicitly
   mid-algorithm) and no random/unordered iteration; given the same CSVs and the same `as_of`, and the
   same (cached, content-addressed) fetched-candle evidence, two runs produce byte-identical output.
   Newly fetched OANDA candles are cached to disk, content-addressed by request parameters, exactly like
   the existing audit tool — a rerun against the same cache never re-fetches, and a rerun without network
   access at all still reproduces the same result from the cache.

## 4. Classification taxonomy (brief's section 6, implemented exactly)

`reconstructed_targeted`, `reconstructed_stopped`, `reconstructed_time_exit`,
`unresolved_insufficient_historical_data`, `unresolved_ambiguous_price_sequence`,
`unresolved_missing_original_trade_parameters`, `unresolved_measurement_contract_incompatibility`.
The original production classification is carried alongside every row under `original_state`
(never overwritten, never blended into the same field as the reconstruction result).

## 5. Validation controls (brief's section 7) — plan, pending real data

Once the real CSVs arrive: replay all 4 live-completed trades and both previously-reconstructed trades
through this engine using the same historical evidence; compare exit classification, exit price, exit
timestamp and R to the originals. **Tolerance, fixed in advance (not tuned after seeing results):**
exit price exact match to 5 decimal places (matching OANDA's own quoted precision) when the same sample
is used; exit timestamp exact match to the second; R multiple within `1e-9` relative tolerance (floating
point only, not a modeling tolerance) — any discrepancy beyond that is a reported finding, not something
adjusted to force agreement, per the brief's explicit instruction.

## 6. What's built now, ready for the real data

- `independent_reconstruction/engine.py` — the reconstruction engine (CSV loaders, validators, the
  chronological walk, classification, determinism via content-addressed candle caching).
- `independent_reconstruction/historical_sources.py` — a small `HistoricalSource` interface with one
  concrete implementation (`OandaPracticeSource`, GET-only, practice-environment-enforced, same bounded
  request discipline as the existing audit tool) and the Dukascopy module already in `src/data/`
  documented as a future alternate implementation if ever needed, not wired in.
- `independent_reconstruction/tests/test_engine.py` — synthetic-fixture tests covering every
  classification path, determinism, UTC normalization, and all four fabrication-refusal rules in
  section 3.8 above.
- `independent_reconstruction/performance.py` — the three required performance views (brief section 8):
  win rate, total/average R, profit factor, max realised drawdown, by-pair and by-direction breakdowns,
  cost-sensitivity sweep, all computed only over resolved trades with unresolved counts reported
  separately. `independent_reconstruction/tests/test_performance.py` covers it against synthetic data
  (34 tests total across both suites, all passing as of this writing).

## 7. Reproduction

```bash
cd independent_reconstruction
../.venv/bin/python -m pytest tests/ -q
```

Running the real reconstruction once the CSVs exist (not done yet):

```bash
../.venv/bin/python engine.py \
  --input codex_audit_exports/V2_RECONSTRUCTION_INPUT.csv \
  --evidence codex_audit_exports/V2_OBSERVATION_EVIDENCE.csv \
  --manifest codex_audit_exports/EVIDENCE_MANIFEST.md \
  --out-dir independent_reconstruction/output \
  --fetch-historical-candles   # opt-in, OANDA practice GETs only, as in the existing audit tool
```
