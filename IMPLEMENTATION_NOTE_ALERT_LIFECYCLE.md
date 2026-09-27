# Implementation note — prospective alert lifecycle

Branch: `alert-lifecycle-prospective-scoring`. Written before coding, per instruction.

## Exact existing paths this touches or reads

| Path | Role today | Change made |
|---|---|---|
| `live_scanner.py` | Active H4 scanner; `log_signal()`/`push_alert()` write `signals_log/*.jsonl`, `alerts.json`, `live_scan.json` | **Additive only** — one new call into the new lifecycle recorder, wrapped in `try/except Exception` so a bug in new code can never break the existing write path. No existing call, field, or file it writes is touched. |
| `streaming_scanner.py` | Disabled legacy scanner (M30/28-pair) | **Not touched.** It will never run again; wiring it in would add risk for no benefit. |
| `src/combiner.py`, `src/strategies/*`, `src/sessions.py` | Signal generation (technical score, PESTLE, SL/TP, session windows) | **Not touched.** No threshold, weight, or generation logic changes anywhere. |
| `performance_scorer.py`, `performance.json` | Existing trade-outcome/directional scorer and its output | **Not touched.** Keeps running exactly as-is against `signals_log`. |
| `dashboard_server.py` | Live dashboard (reads `live_scan.json`/`alerts.json`/`performance.json`) | **Additive only** — one new read-only JSON route added; nothing existing is modified, and the service is not restarted or redeployed as part of this change. |
| `src/data/oanda.py` | `fetch_oanda_candles`, `fetch_current_price` — midpoint-only OANDA client | Read-only reuse, unchanged. Confirms bid/ask is **never** available from this client (`price="M"` only) — the new schema must record that as an explicit unavailability, not invent it. |
| `tests/test_sessions_and_orb.py` | Existing test convention: plain `assert`, no pytest dependency, `if __name__ == "__main__":` runner | New tests follow the same convention. |

## New files (all additive; nothing above is renamed, deleted, or rewritten)

- `src/alert_lifecycle.py` — event schema, append-only store, revision/cancellation logic, repeated-poll classifier.
- `src/alert_scorer.py` — scores each published alert **version** against the exact levels/timing published for that version.
- `src/alert_performance_view.py` — aggregation (issued/actionable/expired/cancelled/open/time-exited/stopped/targeted, denominators visible, split by `scanner_version` — never blended), plus a read-only legacy adapter for `signals_log`/`performance.json` that never fabricates a field the old system doesn't have.
- `tests/test_alert_lifecycle.py` — required test cases (listed at the end).
- New data file at runtime (not created by this commit, only by running code): `alert_lifecycle_log.jsonl` — append-only, alongside but independent of `signals_log/`.

## Event schema (`alert_lifecycle_log.jsonl`, one JSON object per line, append-only, never mutated)

Every event carries: `event_id` (uuid4), `event_type` (`issued|revised|cancelled`), `alert_id` (uuid4 — a **new** id for `issued` and `revised`; `cancelled` references an existing one), `lineage_id` (constant across a whole issued→revised→…→cancelled chain — equals the original `alert_id`), `scanner_version`, `pair`, `direction`, `recorded_at_utc` (when this event itself was appended — the true "publication time" for this event, since there is no separate delivery step in this system).

`issued` / `revised` additionally carry the **immutable original levels for that specific version** — once written, a later revision creates a **new** record; it never edits this one:
```
calculated_at_utc, technical_inputs {orb,trend,pattern,composite}, pestle_inputs (or null + pestle_used:false),
quoted_midpoint, bid_ask_observed (always null today — see below), bid_ask_available (bool),
entry_price, entry_tolerance, entry_condition_lo, entry_condition_hi,
stop, target, entry_expiry_utc, max_holding_time_hours,
confidence, combined_score, reason,
revises_alert_id (null for issued), revision_reason (null for issued)
```
`bid_ask_observed` is `null` and `bid_ask_available` is `false` on every record produced today — `fetch_current_price`/`fetch_oanda_candles` only ever return OANDA's midpoint (`price="M"`), confirmed by reading `src/data/oanda.py`. The schema has the field so it stops being silently absent the moment a real bid/ask feed exists; until then it is marked unavailable, never backfilled from `src/spreads.py`'s static snapshot (that snapshot is a cost-modelling input for `combiner.py`'s stop floor, not an observed quote, and conflating the two was flagged as a risk in the read-only audit).

`cancelled` carries: `cancelled_alert_id` (the version being cancelled), `cancellation_reason`.

## Entry-window anchoring fix (new code only — `sessions.py` itself is untouched)

`entry_expiry_utc = min(published_at + DEFAULT_VALIDITY_MINUTES, session_close)`, **but only when `session_close > published_at`** — i.e. this module reuses `sessions.session_close_datetime()` but adds the guard that `sessions.signal_window()` (used elsewhere for display metadata only) does not have, which is exactly the "session close already past" bug the read-only audit traced (§1 of `CODEX_FOLLOWUP_FINDINGS.md`). This is a new, additive safeguard in the new alert module, not a change to the existing `sessions.py`/`combiner.py` signal-generation code path.

## Repeated-poll / confidence-flicker classifier

For a pair with an active (not yet cancelled/expired) lineage, a new candidate is classified as:
- **cosmetic refresh (no record written at all)** — same direction, same confidence tier, entry/stop/target each within `ENTRY_REFRESH_TOLERANCE_ATR_MULTIPLE` (a named constant, flagged below as a decision point) of the active version's own levels. This is what "repeated polls / confidence flicker" produce on an unchanged setup — nothing is written, so nothing can silently replace the alert a subscriber is already looking at.
- **revision** — same direction, but confidence tier changed or a level moved beyond tolerance. Linked to the prior version via `revises_alert_id`; the prior version's own record is never edited, and its *effective* entry window is closed at the revision's timestamp (a subscriber only ever sees the latest version, so the old one can no longer be entered from that instant on).
- **cancellation** — the scanner's direction reverts to `no_trade` (or flips sign — a reversal is a different trade thesis, not an update to the same one: it cancels the old lineage and starts a new one).
- **new issuance (fresh lineage)** — no active version exists for the pair (first fire, or the previous lineage already reached its own `entry_expiry_utc`/was cancelled).

## State model (computed by the scorer by replaying a lineage's events — never stored as a mutable field)

For each `issued`/`revised` version: `effective_entry_window_end = min(entry_expiry_utc, next_version's recorded_at_utc if any, cancellation's recorded_at_utc if any)`.

```
issued/revised  →  [cancelled before effective_entry_window_end]        → cancelled
                →  [price never reaches entry_condition range
                     before effective_entry_window_end]                  → expired_no_entry
                →  [price reaches entry_condition range]                 → assumed_entered
                       assumed_entered → [target hit first]               → targeted
                       assumed_entered → [stop hit first]                 → stopped
                       assumed_entered → [neither, by assumed_entry_time
                                          + max_holding_time_hours]       → time_exited
                       assumed_entered → [still within holding window,
                                          not yet resolvable]             → open
```
"Assumed entered" is exactly that — an assumption the scorer makes for scoring purposes only, stated in every scored record's `entry_basis` field; Signal IQ has no record of whether any subscriber actually entered. `entry_expiry_utc` ("do not enter after this time") and `max_holding_time_hours` (a *relative* duration, anchored to the assumed entry moment, not to publication) are deliberately separate fields so the two questions in requirement 3 stay distinct in the schema itself, not just in prose.

## Counts for the dashboard (requirement 5), always split by `scanner_version`, denominators shown

`issued` = every issued/revised record. `actionable` = issued with a non-zero effective entry window. `expired` / `cancelled` / `open` / `time_exited` / `stopped` / `targeted` as in the state model above. The current H4 config (`scanner_version` containing the live `live_scanner.py` deploy commit) is never combined with the legacy M30/28-pair scanner_version's counts in any aggregate — they are separate dict keys, always, including when there are only 3 (or 0) issued alerts for a version.

## Legacy data (requirement 6)

`src/alert_performance_view.py` includes a **read-only adapter** that presents old `signals_log`/`performance.json` rows in a compatible reporting shape for side-by-side display, tagging `alert_id: null`, `lifecycle_schema: "legacy_v0_no_alert_id"`, and every field this old data never had (`bid_ask_observed`, true publication-vs-calculation distinction, entry-condition range) as `"not_recorded"` — never invented. This is pure read-time transformation; `signals_log/*.jsonl` and `performance.json` are not migrated, rewritten, or touched by any file in this branch.

## Tests to write (`tests/test_alert_lifecycle.py`, plain `assert` + `__main__` runner, matching existing convention)

1. Immutable original levels (revising never edits the original record).
2. A linked revision (correct `revises_alert_id`, correct reason, prior version's effective window closes at the revision time).
3. Cancellation (terminal, no further versions in that lineage).
4. Expiry without entry (price never in range before `effective_entry_window_end`).
5. Time exit (assumed entered, neither level hit within `max_holding_time_hours`).
6. Repeated polls produce **no** spurious revision (cosmetic-refresh path, using a fixture built from the audit's own duplicate-group data — same pair/session/direction, price ticking a few pips between polls).
7. Scorer attribution to the correct alert **version** (a lineage with a revision; confirms the original version is scored against its own original levels, not the revision's).
8. Dashboard separation of legacy vs. current `scanner_version` in the aggregation output.
9. A fixture whose session close is already past at publication time (the audited bug) — confirms the new module's guarded `entry_expiry_utc` does **not** reproduce it, unlike the existing `sessions.signal_window()`.

## Decisions for Jase and Codex before any deployment

1. **`ENTRY_REFRESH_TOLERANCE_ATR_MULTIPLE`** (repeated-poll vs. revision boundary) — set here to a placeholder or real value? This directly determines how often subscribers get a new notification vs. a silent no-op on ordinary price noise.
2. **`DEFAULT_ENTRY_VALIDITY_MINUTES`** and **`max_holding_time_hours`** defaults — currently mirrors `sessions.DEFAULT_SIGNAL_VALIDITY_MINUTES` (90) for entry validity and `performance_scorer.MAX_LOOKAHEAD_HOURS` (30) for holding time, for continuity with the existing system, not because either was re-validated here.
3. **Bid/ask**: this change does not add a real bid/ask feed. Every scored result is explicitly labelled an estimate against midpoint prices. Deciding whether/when to add a real quote feed is out of scope for this change.
4. **Notification delay**: scored as 0 seconds (subscriber assumed to see the alert at its `recorded_at_utc`) — a stated assumption, not a measurement, since no delivery timestamp exists anywhere in this system.
5. **Whether/when to wire this into `dashboard_server.py`'s actual rendered HTML** (this change adds a JSON route only, not a new visual tab) — a presentation decision that doesn't need to block the data-layer/scoring correctness this change is about.
6. **Whether to ever backfill `streaming_scanner.py`'s legacy data into real lifecycle records** — this change deliberately does not, per requirement 6; if ever wanted, it would need real evidence of what "one instance" vs. "a repriced instance" meant historically, which (per the read-only audit) is not reliably recoverable from `signals_log` alone (812 of 818 duplicate-key groups have genuinely different levels across members).

This change does not validate, invalidate, or re-tune any trading strategy. It changes how an alert's own published instruction is recorded and scored — nothing about signal generation.
