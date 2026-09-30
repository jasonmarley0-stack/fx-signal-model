# Prospective baseline observation — Stage 2

Isolated research code. Writes only to `research/prospective_baseline/logs/` and `research/prospective_baseline/output/` — never to `signals_log/`, `alerts.json`, `performance.json`, `alert_lifecycle_log.jsonl`, or any file the public scanner or dashboard reads. Places no order. Not started, deployed, or merged by this change.

**Practice-only is enforced in code, not only in this document.** `observe.py`'s `main()` calls `run_identity.enforce_practice_environment()` before doing anything else and refuses to start (raises `PracticeEnvironmentError`) unless `OANDA_ENVIRONMENT` is exactly `practice`. It also calls `run_identity.load_or_create_manifest()`, which persists a `run_manifest.json` (run id, start time, a SHA-256 hash of this system's own code plus the production strategy files it reuses) in the log directory; if that code/contract hash ever differs from what a run started under, the process refuses to continue (`RunIdentityMismatchError`) rather than silently blending observation data collected under different logic into one run. See "Activation integrity" below.

**Strategy under observation:** the frozen baseline only — `src/strategies/composite.py`'s `technical_score()` and `src/combiner.py`'s `combine_signal()`, called directly (not reimplemented), same as Stage 1's winning candidate. Stage 1 already rejected genuine_orb and trend_pullback as tested; they are not reused here.

Read `contract.py` before reading anything else — it states exactly how this stage's execution assumptions differ from the offline replay's (rolling vs. expanding warm-up, real vs. zero decision latency, discretely sampled quotes vs. continuous M30 candles). Those two contracts are never blended; a number from one is never compared directly to a number from the other without that difference stated.

## What's ready

| Component | File | Status |
|---|---|---|
| Observation contract | `contract.py` | Frozen, documented |
| OANDA pricing client | `quote_client.py` | Pure parsing tested; real network call not exercised without credentials |
| Collector (decision + quote ticks, restart-safe) | `observe.py` | Implemented, unit-tested against injected fixtures; `main()`'s live loop not run in this session |
| Paper scorer | `score.py` | Implemented, unit-tested |
| KPI report | `report.py` | Implemented, tested end to end against a synthetic run |
| Activation integrity (practice-only guard, run identity) | `run_identity.py` | Implemented, unit-tested |
| Tests | `test_observation.py` | 26 tests, all passing, no network/credentials |

**Not run against live data or a real practice account in this session.** Every number produced by `test_observation.py` is from synthetic or historical-replay fixtures, explicitly to verify the harness's own logic — see each test's docstring. No strategy performance claim is made here; that only becomes possible once real observation data exists (see "First review" below).

## Data model

- `logs/decisions_log.jsonl` — one row per baseline decision: `source_candle_start_utc`, `source_candle_completion_utc` (start + 4h, the H4 candle's own fixed duration — the earliest moment this decision could exist), `actual_calculation_time_utc` (when this process actually computed it — real, measured, can lag completion by network/poll delay, never assumed zero), `actual_recording_time_utc`, `decision_delay_seconds`, direction/confidence/entry/stop/target, `technical_inputs` (orb/trend/pattern — orb confirmed structurally 0 on H4, same as production, see the read-only audit), `entry_expiry_utc`, `max_holding_time_hours`.
- `logs/quotes_log.jsonl` — one row per sampled quote (`QUOTE_SAMPLE_INTERVAL_SECONDS`, currently 5s): pair, `received_at_utc` (captured **after** the HTTP response returns, not before the request is sent — this is what every freshness/coverage check below actually uses), `request_started_at_utc` (preserved separately, for diagnosing slow requests only), `oanda_time_utc` (OANDA's own quote timestamp), bid, ask, `tradeable` (OANDA's own dealability flag).
- `logs/health_log.jsonl` — `poll_failed` / `quote_poll_failed` / `no_signal` / `no_data` / `*_crashed` events — a gap in observation is always visible here, never silently absent.
- `logs/.observe_state.json` — last-decided source-candle-completion per pair, for fast restart lookup; `decision_already_recorded()` also checks the actual log directly, so a lost/stale state file cannot cause a duplicate.
- `logs/run_manifest.json` — this run's identity: `run_id`, `started_at_utc`, and the source hash `load_or_create_manifest()` checks on every start (see "Activation integrity").

## Scoring discipline (`score.py`), same principles as the offline replay, adapted for discrete samples

- **Quote validity, checked before a quote may establish anything** (`_valid_pair_quotes()`): real bid+ask, `tradeable=True`, a parseable `oanda_time_utc` no older than `QUOTE_MAX_PROVIDER_AGE_SECONDS` relative to `received_at_utc`, and `received_at_utc` no later than the scoring clock (`now`) — a sample "received" after the moment being scored is never used, whatever the reason. A missing/unparseable provider timestamp, or one that's too old, makes the quote unusable — explicit uncertainty, never silently trusted as fresh.
- Fill side: a long's entry is checked against ASK samples, exit against BID; a short is the mirror — charges the real observed spread exactly once.
- Same-sample exit: if the sample that confirms entry ALSO already shows its own exit-side price past stop or target, that's recorded (`same_sample_exit: true`) and priced at the **actually observed** exit-side quote — never the idealised stop/target level, since a discrete sample gives no evidence a resting order would have filled exactly there.
- Both levels breached in the same sample → `ambiguous_intrabar_exit`, never guessed.
- A genuine coverage gap during the entry window (no samples, samples too far apart, or a gap trailing to the window's end) → `insufficient_data_entry`, distinguished from a real, fully-covered miss (`expired_no_entry`). `_has_coverage_gap()` checks the leading, every intermediate, **and** the trailing gap to the window's end — a single early sample followed by silence for the rest of a window is a gap, not full coverage.
- The exit scan (once a position is entered) is itself gap-aware: it tracks the gap since the last checked sample and returns `incomplete_coverage` the instant a candidate sample is preceded by more than `QUOTE_STALENESS_SECONDS` of silence — a "clean" stop/target hit arriving after an unobserved stretch (e.g. a 30-minute gap) is never reported as a confirmed win or loss.
- **Holding deadline: one consistent rule.** Gap-free preceding coverage is required up to the deadline itself (checked with the same `_has_coverage_gap()`), then the time exit is priced at the **first valid sample at or after** `max_exit_time` (within `QUOTE_STALENESS_SECONDS` of it) — never the last sample before the deadline. The actual `exit_time_utc`, the nominal `scheduled_exit_time_utc`, and the real `execution_delay_seconds` between them are all recorded. Once that sample has priced the exit, no later stop/target movement can reclassify it. Missing coverage at the deadline → `incomplete_coverage`, same as everywhere else — never a fabricated fill.
- **Position policy** (`build_paper_ledger()` in `score.py`, used by `report.py` instead of the no-suppression `score_all()`): decisions for the same pair are walked chronologically, and a later decision that begins while an earlier one's position is still open is marked `executable: False`, `state: "suppressed_existing_position"` and excluded from opportunity counts. An entered position with an unresolved outcome (ambiguous/incomplete-coverage/open) reserves the pair conservatively through `entry_time + max_holding_time_hours`, the same conservative-occupancy discipline `research/offline_comparison/replay_engine.py` uses. `score_all()` is retained separately for inspecting raw per-decision scoring with no suppression applied.

## Reproducible KPI report

```bash
.venv/bin/python3 research/prospective_baseline/report.py
```

Reads `logs/` (empty until observation actually runs — see Activation below), applies the position-policy paper ledger (`build_paper_ledger()`), scores every decision, writes `output/kpi_report.json` and prints: eligible alerts by month (plus how many were suppressed by the position policy), entered/completed/pending/unknown counts, avg net R per completed trade (with its denominator), avg net R per **all eligible** alerts (total completed R divided by every eligible alert, including confirmed missed entries at zero — reported explicitly `undetermined`, with the denominator still stated, whenever any outcome is unknown; never silently substituted with the completed-only average), partial completed-trades-only drawdown (labelled, not an account-percentage figure), decision and execution (deadline) delays, quote-sample counts, recording failures, and an explicit `unobserved` list: financing/swap, true fill slippage, and between-sample price movement are none of them recorded or estimated anywhere in this report.

## Activation integrity

Enforced in **code**, not only in this document:

- `enforce_practice_environment()` (`run_identity.py`) is called first thing in `observe.py`'s `main()` and raises `PracticeEnvironmentError`, refusing to start, unless `OANDA_ENVIRONMENT` is exactly `practice` — missing, `live`, or any other value all refuse.
- `load_or_create_manifest()` persists `logs/run_manifest.json` (run id, start time, a SHA-256 hash over `contract.py`, `observe.py`, `score.py`, `src/combiner.py`, `src/strategies/composite.py`, `src/spreads.py`) on first use of a log directory. On every subsequent start it recomputes the current hash and compares; a mismatch raises `RunIdentityMismatchError` and refuses to proceed — an intentional code or contract change must start a **new** log directory (a new run), never silently continue blending data collected under different logic into the same logs.
- `observe.py` only ever writes to `research/prospective_baseline/logs/`, an isolated checkout's own directory — it never touches `signals_log/`, `alerts.json`, `performance.json`, or the public scanner's active checkout. Activation must run from its own isolated checkout with its own log directory; this change does not switch or merge the public scanner's active checkout, and activation must not either.

## Practice-only smoke check (run manually, once, before any supervised start)

Uses the **existing** private credential handling (`setup/oanda.env`, already used by `live_scanner.py`/`data/oanda.py` — nothing new is introduced) and OANDA's **practice** environment only. Never prints a credential value.

```bash
cd /root/fx-signal-model   # or wherever this ISOLATED checkout lives on the host that has setup/oanda.env
set -a; source setup/oanda.env; set +a
echo "OANDA_ENVIRONMENT=${OANDA_ENVIRONMENT:-<unset>} (must be exactly 'practice' — observe.py now refuses to start otherwise)"
.venv/bin/python3 -c "
import sys; sys.path.insert(0, 'research/prospective_baseline')
from quote_client import fetch_pricing_samples
samples = fetch_pricing_samples(['EURUSD'])   # receipt time is always this call's own post-response clock reading -- no override accepted
print('quote sample OK:', {k: v for k, v in samples[0].items() if k != 'pair'} if samples else 'NO SAMPLE')
"
.venv/bin/python3 -c "
import sys; sys.path.insert(0, 'research/prospective_baseline'); sys.path.insert(0, 'src')
from datetime import datetime, timezone
from observe import _fetch_rolling_h4_mid, compute_decision
df = _fetch_rolling_h4_mid('EURUSD')
print('rolling H4 fetch OK, bars:', len(df))
print('decision (may legitimately be None):', compute_decision('EURUSD', df, datetime.now(timezone.utc)))
"
.venv/bin/python3 -c "
import sys; sys.path.insert(0, 'research/prospective_baseline')
from run_identity import enforce_practice_environment, PracticeEnvironmentError
try:
    enforce_practice_environment()
    print('practice-only guard OK: OANDA_ENVIRONMENT=practice confirmed')
except PracticeEnvironmentError as ex:
    print('practice-only guard REFUSED to proceed:', ex)
"
```

If all three print cleanly (no traceback, no credential value visible, and the guard confirms `practice`), the collector's two real network paths work and the code-enforced practice-only check passes. This performs GET requests only — `PricingInfo` and `InstrumentsCandles` — no order endpoint is called anywhere in this system. Starting `observe.py` itself (not done by this smoke check) will additionally create or validate `logs/run_manifest.json` on its first line of output.

## Process supervision (not installed by this change)

`prospective-baseline-observer.service`, to be copied to `/etc/systemd/system/` and enabled manually by whoever activates observation — mirrors `setup/dashboard-server.service`'s existing persistent-process pattern exactly (`Type=simple`, `Restart=on-failure`, the same `EnvironmentFile=` mechanism, nothing new):

```ini
[Unit]
Description=Prospective baseline observation (Stage 2, paper-only, no orders — see research/prospective_baseline/README.md)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/fx-signal-model
EnvironmentFile=/root/fx-signal-model/setup/oanda.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/root/fx-signal-model/.venv/bin/python3 /root/fx-signal-model/research/prospective_baseline/observe.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## Exact activation action

1. Deploy this branch to its own **isolated checkout** with its own `logs/` directory (not the public scanner's active checkout — do not switch or merge it) and its own `setup/oanda.env` (not done by this handoff).
2. Confirm `OANDA_ENVIRONMENT=practice` in that file (this system must never point at a live/funded account) — `observe.py` now refuses to start otherwise, in code.
3. Run the practice-only smoke check above once, manually, and read its output.
4. Copy the service file above to `/etc/systemd/system/prospective-baseline-observer.service`, then:
   ```bash
   systemctl daemon-reload
   systemctl enable --now prospective-baseline-observer.service
   journalctl -u prospective-baseline-observer.service -f   # confirm it's ticking, no credential ever appears in the log
   ```
   The first line of output is `Run identity: run_id=... source_hash=...` — confirms the practice-only guard passed and `logs/run_manifest.json` was created (first start) or validated against the current code (subsequent starts). None of this is run by this handoff — it is the exact next action for whoever activates observation.

## Review cadence

- **First data-quality review: 7 days after activation.** Read `report.py`'s `operational` block — decision-delay distribution, quote-sample coverage, recording-failure count. This checks the *collector*, not the *strategy* — a clean 7-day run with no unexplained gaps is what "ready to keep observing" looks like; it is not a performance claim.
- **Performance review: 30 days after activation, or at 50 completed outcomes if reached first.** Read the full KPI report — completed-trade average R, the undetermined all-eligible figure, partial drawdown, by-month breakdown. These are progress checkpoints, matching Stage 1's own discipline (a positive result is a reason for further observation, not validation) — not a claim that the strategy is ready for Stage 3 (bounded practice execution).

No strategy tuning occurs before, during, or as a result of this review cadence — any parameter change would be a new, separately-labelled contract, exactly as Stage 1 required.
