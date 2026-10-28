# Bounded publication and supplementary gap evidence — 28 October 2026

## What changed (not deployed)

The publisher now maintains a **disposable, publisher-owned SQLite index**
outside the frozen observer. It incrementally indexes complete JSONL lines,
then loads **one decision window**, never all history for a pair. It executes
the frozen ledger function's actual bytecode with a local score adapter that
calls the actual frozen scorer on that window. No module globals, observer
source, manifest, scoring constants or recorded outcomes are modified.
The full health log is streamed rather than accumulated in Python.

The cache handles append/restart, partial final lines and rotation/truncation.
Its checkpoint and inserts commit together. It is not evidence: loss merely
requires rebuilding from the original logs. Arbitrary in-place historical
edits are unsupported; rebuild the cache after any such edit. Keep all
publisher invocations under the existing shared service lock. SQLite uses a
2 MiB page cache and disk-backed temporary sorting. A 100,000-row per-window
safety guard fails explicitly before unexpectedly dense input can exhaust RAM.

This bounds **quote-history** memory, not all possible resource use: decision
and ledger metadata still grow with alerts, the disk index grows with raw
history, health-history scanning remains linear, and total rescoring time
grows with decisions. Monitor disk space and duration. Do not call this an
infinite-capacity system or remove the existing cgroup safeguards.

## Measured local, end-to-end publisher results

Each run is a fresh subprocess, real pinned-v2 fixture functions, dense 5-second
same-pair data, full `publish_kpi_snapshot()` including health aggregation.
These are **synthetic engineering benchmarks**, not production measurements
or evidence of strategy profitability.

| Same-pair decisions | Relevant quotes | Health rows | Peak RSS | Publish time |
| --- | ---: | ---: | ---: | ---: |
| 10 × 30h | 216,010 | 216,010 | 44.10 MiB | 2.38s |
| 50 × 30h | 1,080,050 | 1,080,050 | 44.25 MiB | 11.88s |
| 1 dense 94h window | 67,681 | 67,681 | 98.27 MiB | 0.75s |

Reproduce with `python tests/measure_bounded_history.py 10`, `... 50`,
and `... 1 94`. Count grows fivefold without retaining five times the
pair's quotes in Python. The 94h case explicitly includes the unconditional
closure-search margin which previous sparse benchmarks missed.

## Measurement: separate evidence, not relaxed live scoring

`research_gap_evidence.py` defines `supplementary_gap_evidence_v1`.
It anchors to the original v2 assumed entry, original levels, original holding
duration and original suppression. **It is a supplementary reconstruction,
not a newly simulated strategy or prospective validation.** It never overwrites
`research_kpi_v2.json` or imports results into live performance.

For a >15-second interval, either:

1. Valid, explicitly `tradeable=false` receipts continuously explain the
   whole interval (any weekday, including daily rollover); or
2. Complete historical **exit-side** broker candles cover the whole missing
   interval and their entire high/low envelopes exclude **both** stop and
   target. Bid is used for longs, ask for shorts. Provenance and fetch time
   are retained. Missing bars, corrupt/future evidence, or any potential
   level touch leave the outcome unknown.

There is **no larger absence tolerance**, no interpolated quote, no stop fill
invented from a bar, and no guessed intrabar ordering. A subsequent observed
quote supplies an exit. A quote at/after the real deadline is still required;
an old candle close never substitutes for it. A fully evidenced closure can
delay execution to reopening, with stop/target precedence and the actual
observed reopening price, including losses worse than −1R.

Daily non-tradeability handling expands v2 policy, and historical supplements
are information obtained later. For these reasons this contract is separate
and clearly labelled. It does **not** magically make every unknown recoverable,
recover unobserved execution, or validate first-touch accuracy between polls.
Financing, commission and real slippage remain unaccounted for.

## Host-connected activation (not executed here)

1. Verify actual deployed code/status and preserve unrelated changes. Merge
   this reviewed repair into `main`; do **not** switch the shared checkout
   to a research branch. Deploy `research_snapshot_publisher.py` together
   with its new `research_quote_index.py` dependency. No observer update.
2. Retain serialization, resource protections and v1's paused KPI timer.
   Pause only v2 KPI publication while installing, if necessary. Check free
   disk space: the first cache build writes a second indexed copy of quotes.
3. Run a controlled v2 publication using the existing unit. Measure host RSS,
   time, snapshot identity and quote continuity. Compare its full ledger
   locally against the frozen scorer before restoring the cadence. Confirm
   the next automatic publication too. Do not lower memory caps from local
   figures alone; no observer restart or new run identity is needed.
4. On the same deployed code, use the existing practice environment and run:

   ```sh
   .venv/bin/python research_evidence_audit.py \
     --observer-checkout /root/fx-signal-model-stage2-observer-v2 \
     --out-dir /root/fx-signal-model/research_supplementary \
     --fetch-gap-candles
   ```

   Default without `--fetch-gap-candles` is offline. With it, only practice
   instrument-candle GETs occur, only for unresolved gaps, at most 20 requests
   per entered decision and 4,990 S5 bars per request, each with an 8s request
   timeout. Errors preserve unknowns. No order endpoint exists in this tool.
   Requests use BA, never midpoint fallback. Raw successful responses are
   content-addressed in `candle_evidence/`; the separate report includes its
   own source hash and the unchanged original run ID/source hash.
5. Read `research_supplementary_evidence.json`: show original and derived
   states separately with each proof or remaining unproved interval. Do not
   turn these retrospective results into live KPI completions. Deciding
   whether to adopt a revised prospective measurement contract is a separate
   explicit decision; it is **not** silently deployed by this repair.

Rollback: revert the reviewed repair and restore the previous publisher as
a matched deployment. The new SQLite files may remain unused; never delete
raw observer logs. No dashboard/strategy/service-unit change is required.

## Verification boundaries

The review branch is based on deployed-code reconciliation `c47370a`, not a
merge of the research branch. It includes only publisher/audit code, tests,
read-only pinned-v2 test fixtures and this note; no dashboard or observer
source changes. Main-based checks: **57 passed** (new bounded/evidence tests,
existing publisher/dashboard/session/data tests). The unchanged research
branch's v2 observation, real-client-path and session/data tests separately
passed **42 checks**. There is overlap in those groups; do not sum them as
99 unique tests. Python compilation and `git diff --check` also pass.

Tests cover frozen v1/v2 ledger parity (including closure and suppression),
incremental cache checkpoints, bounded-window guards, single-pass health
counts, supplementary evidence positive/negative cases, practice-only GETs,
side selection, exact deadline handling, reopening gaps and log immutability.
No production credentials, runtime logs, new production observations or
host deployment were accessible in this execution environment. Actual
recovery of the three production unknowns remains **unverified** until the
host-connected audit runs. No dashboard outcome counts have changed here.
