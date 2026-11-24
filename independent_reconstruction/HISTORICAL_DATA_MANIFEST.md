# Historical Data Manifest

Tracks every historical-data source actually consulted for this reconstruction, and the exact
provenance of anything fetched. Updated as the exercise proceeds — this is the current state as of
2026-11-24, before the real trade/evidence export has arrived.

## Sources assessed (see `HISTORICAL_DATA_SOURCE_ASSESSMENT.md` for the full writeup)

| Source | Status | Credentials used | Data retained |
|---|---|---|---|
| OANDA practice v20 API | Selected, verified reachable for the full scope window | Existing project credentials (droplet-side `setup/oanda.env`; never copied off the droplet, never printed) | None yet — only availability probes were run, each a few S5 candles, immediately discarded. No cache files exist under `independent_reconstruction/cache/` yet. |
| v1 observer's `quotes_log.jsonl` | Selected as secondary/cross-validation, confirmed to cover the full window | None (read-only, already-collected data on the droplet) | Not yet pulled — will only be consulted for specific trade windows once the real trade list exists. |
| Dukascopy (`src/data/dukascopy.py`) | Assessed, connectivity unreliable from both available networks today | None | None. No tick data retained. |
| HistData.com | Assessed from public documentation and reachability check only | None | None |
| Twelve Data | Assessed via the public demo endpoint only | None (no account created, no API key requested) | None — the one response received (today's live bars, not historical) was not saved. |

## Fetches performed so far

All of the following were read-only GET requests, OANDA practice environment enforced, run today
(2026-11-24) purely to validate availability for this assessment — none are part of the committed
reconstruction output and none were cached:

- 7 pairs × 3 probe windows (2026-10-26 09:00, 2026-11-01 00:00, 2026-11-20 23:55–21) at S5/BA — see
  `HISTORICAL_DATA_SOURCE_ASSESSMENT.md` section 1 for results.
- 2 pairs × 1 additional probe (2026-11-20 14:00, Friday midday) confirming end-of-window coverage.
- Three Dukascopy CDN reachability checks (one in-window-dated, two real-calendar-dated) — all failed
  (rate-limited, then connection timeout).
- One Twelve Data demo-key request, one HistData.com reachability check (both just confirming the
  domain responds; neither pulled usable historical data for this window).

No disk-persisted historical price data exists yet. Once the real reconstruction runs with
`--fetch-historical-candles`, every OANDA response will be cached under `independent_reconstruction/cache/`
as a JSON file content-addressed by its exact request parameters (pair/from/to/granularity), with its
own `fetched_at_utc` recorded — the same provenance discipline already used in the production
supplementary-evidence audit (`research_evidence_audit.py`).

## Phase 2 update (2026-11-24) — the real export arrived and was used

Received at `/Users/jasonmarley/Documents/ChatGPT/Signal IQ - FX/audit/2026-11-24-v2-observer/`.
Read-only copies imported into `independent_reconstruction/data/` (chmod a-w); originals verified
byte-for-byte unmodified afterward via SHA-256 re-hash. All 13 manifest-listed hashes matched exactly;
the 8 content-addressed `phase2_reconstruction_evidence/*.json` files were independently verified
byte-identical against the live production droplet's own `/root/fx-signal-model/research_supplementary/`
copies (not just internally self-consistent).

**OANDA practice fetches actually performed for the real reconstruction run**: 25 live requests total
across all 22 entered trades (20MB of cached responses, preserved under `independent_reconstruction/
cache/` for offline reproducibility — no network access needed to rerun `engine.py` against this exact
cache). This is far fewer than the pre-padding design would have needed (one request per disqualifying
gap) — the padded-fetch optimization (see `RECONSTRUCTION_METHODOLOGY.md`) meant most nearby small gaps
reused an already-fetched, wider candle response.

`V2_OBSERVATION_EVIDENCE.csv`: confirmed 452,706 data rows, cleanly bucketed across exactly the 22
entered trade_ids (no blank/foreign trade_id rows), ~19,600–20,850 rows per trade. No rows outside the
scoped pairs or outside a trade's own window were present.

Full results: `independent_reconstruction/output/full_reconstruction_results.json` (per-trade),
`V2_RECONSTRUCTED_TRADE_LEDGER.csv` (the required deliverable format). See
`RECONSTRUCTION_VALIDATION_REPORT.md` for what did and didn't validate.
