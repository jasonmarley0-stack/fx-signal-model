# Corrections to the alert lifecycle branch, per Codex's review

Branch: `alert-lifecycle-prospective-scoring`, correcting commit `6bcdd2a`. Same constraints as before: no merge, no deploy, no service restart, no broker orders, no change to strategy weights/thresholds/stops/targets.

**Core acceptance rule restated:** every alert version the performance scorer counts must correspond to the exact actionable instruction published to the user-facing alert feed. A recorded revision or cancellation cannot exist only in `alert_lifecycle_log.jsonl`.

## 1. Unified recording and publication

**Real bug found and fixed, beyond what was asked:** `classify_and_record()` returned only the *last* event it wrote. On a direction reversal it writes two events (cancel the old lineage, then issue the new one) — the caller only ever saw the second one, so the cancellation existed **only** in `alert_lifecycle_log.jsonl`, exactly the failure mode the acceptance rule prohibits. Fixed by having `classify_and_record()` return a list of every event it produces (`src/alert_lifecycle.py`).

`live_scanner.py`'s old gate (`if prev is not None and sig.direction != prev: log_signal(...); push_alert(...)`) was independent of and inconsistent with the lifecycle classifier, which runs every poll. A same-direction revision was recorded in the lifecycle log but never reached `push_alert()` at all. Fixed by removing `push_alert()` and the old gate entirely; `src/alert_feed_publisher.py`'s `record_and_publish()` is now the single call site — it persists via `classify_and_record()` and publishes every resulting event to `alerts.json` using the *same* `alert_id`/`lineage_id`/`event_type`/timestamp/levels, not a second independently-built representation. `signals_log` (legacy) is now driven by the same classification too (fires when a lifecycle event is `issued`/`revised`), not a separate direction-change check.

First issuance now fires correctly (the old `prev is not None` guard — which existed specifically to suppress alerting on a scanner's very first observation — was removed; gating is entirely the lifecycle store's own history now, and the "first issuance" scenario was explicitly required to work).

**Failure between persistence and publication:** `record_and_publish()` catches a publish failure and appends a `feed_publish_result` event (`status: "failed"`, with the error) rather than letting a write succeed while a subscriber notification silently fails. `AlertLifecycleStore.unpublished_events()` finds anything without a `"delivered"` result, and `retry_unpublished()` (called at the top of every `live_scanner.py` poll, plus available standalone) republishes it — recoverable, not just visible. A retried republish does not duplicate an already-delivered entry.

**Requirement 4 (already-entered guidance):** every `revised`/`cancelled` event now carries an `existing_position` snapshot (the *prior* version's own stop/target/max-holding-time), and the published feed entry's `existing_position_guidance` field states explicitly, in the alert itself: *"If you already entered alert X, that trade is unaffected — continue to follow its ORIGINAL stop/target, with a time exit Nh after your entry if neither is hit."* The scorer already didn't truncate a version's post-entry stop/target/time-exit scan at a later revision's timestamp (verified, not just asserted — see the integration test), so this was a disclosure gap, not a scoring one; both are now correct and consistent with each other.

## 2. Entry/exit scoring limits — `src/alert_scorer.py`

- **Entry price:** previously any bar whose high/low merely *touched* the entry range was treated as an entry, priced at that bar's *close* — which can be arbitrarily far outside the entry condition (e.g. a bar that dips through the range and closes well past it). Now only a bar whose own **open** lands inside `[entry_condition_lo, entry_condition_hi]` confirms an entry; a range that's touched but never confirmed by an open returns `insufficient_data_entry`, not a fabricated fill.
- **Boundary bug:** the entry-search window used `<= end`, which could include a bar starting exactly at (or after) the entry-expiry/revision/cancellation boundary. Changed to `< end` (the window is `[start, end)`).
- **Intrabar stop/target ambiguity:** a bar whose range crosses *both* stop and target can't have its order determined from M30 OHLC. Previously this silently assumed stop-first (copying the convention from the older `performance_scorer.py`). Now returns `ambiguous_intrabar_exit` instead of guessing.
- **Time exit with missing coverage:** previously used the last available bar's close as the exit price even if that bar was far earlier than the actual time-exit boundary — a stale price reported as current. Now only prices a time exit if a bar exists within one candle-interval of the boundary; otherwise `incomplete_coverage`.
- Every result stays labelled `result_type: "estimate"`, with the reason for any non-fill state appended to `caveats`.

## 3. Rolling data coverage — `alert_performance_scorer.py`

`fetch_candles_for_events()` previously fetched one window per pair, `[earliest alert, earliest alert + 60 days]` — a version fired more than 60 days after that pair's *first* alert could fall outside its own coverage. Rewritten so each pair's window is sized from **every one of that pair's own versions'** own need (`published_at` through `max_holding_time_hours` + a coverage pad), so a version issued well past 60 days later is still fully covered as history grows — verified with a test that issues one version 70 days after the first and checks the requested fetch range actually reaches it.

Added `bounded_fetch()`, which chunks any span longer than `MAX_SINGLE_FETCH_DAYS` (60) into multiple OANDA requests and concatenates them, rather than requesting an arbitrarily long range in one call.

A fetch failure for one pair is now caught per-pair, recorded in a `fetch_errors` dict, and that pair's versions are scored `fetch_error` explicitly by `score_all()` — not silently blank, and not allowed to stop any other pair from being scored (verified with a test where one pair's fetch fails and the other pair still scores normally).

## 4. Already-entered guidance

Covered under item 1 above — the scoring behaviour (never truncating the post-entry scan at a revision) already satisfied "retain original stop/target/time exit"; what was missing was stating that choice explicitly in the published update, which the `existing_position_guidance` field now does.

## Tests added

- `tests/test_alert_feed_integration.py` — exercises `record_and_publish`/`retry_unpublished` directly (the exact functions `live_scanner.py` calls), not just the lifecycle classifier in isolation: first issue, same-direction revision (with guidance text), cancellation (with guidance text), direction reversal (asserts **both** events reach the feed — this is the regression test for the bug found in item 1), repeated no-op polls (feed provably unchanged), and a simulated publish failure followed by a recovery retry that publishes exactly once (no duplicate, nothing missing).
- `tests/test_alert_scorer_edge_cases.py` — expiry-boundary candle exclusion, intrabar stop/target ambiguity, entry price outside the condition (not fabricated), missing candles at a time exit, one pair's fetch error not blocking another, and coverage extending correctly to a version issued 70 days after the pair's first alert.
- `tests/test_alert_lifecycle.py` (existing suite) — updated only where `classify_and_record`'s return type changed (list instead of single event/`None`); all 9 original assertions unchanged and still passing.

**Local environment note:** running these tests required installing `oandapyV20`/`requests`/etc. from `requirements.txt` into the local `.venv` — they're already declared there but weren't installed locally (present on the droplet). Also confirmed pre-existing and unrelated to this branch: `dashboard_server.py` fails to import under this machine's Python 3.9 (`HTTPBasicCredentials | None` needs 3.10+) — identical failure on `main` at the base commit, not a regression here.

## Remaining limitations, stated plainly

- Still midpoint-only: no real bid/ask feed exists, so every scored result remains labelled `estimate`, never an executable/realised return.
- `insufficient_data_entry`/`ambiguous_intrabar_exit`/`incomplete_coverage` are new, real outcomes a subscriber-facing performance view needs to display honestly (as a labelled category, with its own denominator) rather than silently excluding — `alert_performance_view.py`'s state-to-count mapping passes unmapped states through by name, so they appear, but a human-readable label for each is a presentation decision not made here.
- `retry_unpublished()`'s recovery pass runs at the top of `live_scanner.py`'s own next poll cycle — there is still no separate, always-on retry daemon; if the scanner itself is down, nothing retries until it's back up. This wasn't asked for and would need its own design/ops decision.
- None of this has been run against real OANDA data or deployed anywhere — every test above uses synthetic candle fixtures.
