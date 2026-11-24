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

## Pending

- `V2_RECONSTRUCTION_INPUT.csv`, `V2_OBSERVATION_EVIDENCE.csv`, `EVIDENCE_MANIFEST.md` (Codex) have not
  arrived in `codex_audit_exports/` as of this writing. This manifest will be updated with their
  row counts, date ranges and any cross-checks against this file once they do.
