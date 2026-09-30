# Prospective baseline observation — Stage 2

Isolated research code. Writes only to `research/prospective_baseline/logs/` and `research/prospective_baseline/output/` — never to `signals_log/`, `alerts.json`, `performance.json`, `alert_lifecycle_log.jsonl`, or any file the public scanner or dashboard reads. Places no order. Not started, deployed, or merged by this change.

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
| Tests | `test_observation.py` | 14 tests, all passing, no network/credentials |

**Not run against live data or a real practice account in this session.** Every number produced by `test_observation.py` is from synthetic or historical-replay fixtures, explicitly to verify the harness's own logic — see each test's docstring. No strategy performance claim is made here; that only becomes possible once real observation data exists (see "First review" below).

## Data model

- `logs/decisions_log.jsonl` — one row per baseline decision: `source_candle_start_utc`, `source_candle_completion_utc` (start + 4h, the H4 candle's own fixed duration — the earliest moment this decision could exist), `actual_calculation_time_utc` (when this process actually computed it — real, measured, can lag completion by network/poll delay, never assumed zero), `actual_recording_time_utc`, `decision_delay_seconds`, direction/confidence/entry/stop/target, `technical_inputs` (orb/trend/pattern — orb confirmed structurally 0 on H4, same as production, see the read-only audit), `entry_expiry_utc`, `max_holding_time_hours`.
- `logs/quotes_log.jsonl` — one row per sampled quote (`QUOTE_SAMPLE_INTERVAL_SECONDS`, currently 5s): pair, local receipt time, OANDA's own quote time, bid, ask, `tradeable` (OANDA's own dealability flag).
- `logs/health_log.jsonl` — `poll_failed` / `quote_poll_failed` / `no_signal` / `no_data` / `*_crashed` events — a gap in observation is always visible here, never silently absent.
- `logs/.observe_state.json` — last-decided source-candle-completion per pair, for fast restart lookup; `decision_already_recorded()` also checks the actual log directly, so a lost/stale state file cannot cause a duplicate.

## Scoring discipline (`score.py`), same principles as the offline replay, adapted for discrete samples

- Fill side: a long's entry is checked against ASK samples, exit against BID; a short is the mirror — charges the real observed spread exactly once.
- Same-sample exit: if the sample that confirms entry ALSO already shows its own exit-side price past stop or target, that's recorded (`same_sample_exit: true`) and priced at the **actually observed** exit-side quote — never the idealised stop/target level, since a discrete sample gives no evidence a resting order would have filled exactly there.
- Both levels breached in the same sample → `ambiguous_intrabar_exit`, never guessed.
- A genuine coverage gap during the entry window (no samples, or samples too far apart) → `insufficient_data_entry`, distinguished from a real, fully-covered miss (`expired_no_entry`).
- At the holding deadline: only a sample within `QUOTE_STALENESS_SECONDS` of the deadline is used; anything staler → `incomplete_coverage`, never a fabricated fill from an earlier quote or a look-ahead into a later one.
- A revision/cancellation is out of scope here (Stage 2 observes one strategy, one decision per candle — no lifecycle/position-suppression layer is needed since `observe.py` only ever records what the frozen baseline decided, once per candle, not a stream of competing candidate updates).

## Reproducible KPI report

```bash
.venv/bin/python3 research/prospective_baseline/report.py
```

Reads `logs/` (empty until observation actually runs — see Activation below), scores every decision, writes `output/kpi_report.json` and prints: eligible alerts by month, entered/completed/pending/unknown counts, avg net R per completed trade (with its denominator), avg net R per all eligible alerts (explicitly `undetermined` whenever any outcome is unknown, never substituted), partial completed-trades-only drawdown (labelled, not an account-percentage figure), decision delays, quote-sample counts, recording failures, and an explicit `unobserved` list: financing/swap, true fill slippage, and between-sample price movement are none of them recorded or estimated anywhere in this report.

## Practice-only smoke check (run manually, once, before any supervised start)

Uses the **existing** private credential handling (`setup/oanda.env`, already used by `live_scanner.py`/`data/oanda.py` — nothing new is introduced) and OANDA's **practice** environment only. Never prints a credential value.

```bash
cd /root/fx-signal-model   # or wherever this checkout lives on the host that has setup/oanda.env
set -a; source setup/oanda.env; set +a
echo "OANDA_ENVIRONMENT=${OANDA_ENVIRONMENT:-practice} (expect 'practice' for this smoke check — do not run against 'live')"
.venv/bin/python3 -c "
import sys; sys.path.insert(0, 'research/prospective_baseline')
from datetime import datetime, timezone
from quote_client import fetch_pricing_samples
samples = fetch_pricing_samples(['EURUSD'], datetime.now(timezone.utc))
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
```

If both print cleanly (no traceback, no credential value visible), the collector's two real network paths work. This performs GET requests only — `PricingInfo` and `InstrumentsCandles` — no order endpoint is called anywhere in this system.

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

1. Merge/deploy this branch to wherever `setup/oanda.env` lives (not done by this handoff).
2. Confirm `OANDA_ENVIRONMENT=practice` in that file (this system must never point at a live/funded account).
3. Run the practice-only smoke check above once, manually, and read its output.
4. Copy the service file above to `/etc/systemd/system/prospective-baseline-observer.service`, then:
   ```bash
   systemctl daemon-reload
   systemctl enable --now prospective-baseline-observer.service
   journalctl -u prospective-baseline-observer.service -f   # confirm it's ticking, no credential ever appears in the log
   ```
   None of this is run by this handoff — it is the exact next action for whoever activates observation.

## Review cadence

- **First data-quality review: 7 days after activation.** Read `report.py`'s `operational` block — decision-delay distribution, quote-sample coverage, recording-failure count. This checks the *collector*, not the *strategy* — a clean 7-day run with no unexplained gaps is what "ready to keep observing" looks like; it is not a performance claim.
- **Performance review: 30 days after activation, or at 50 completed outcomes if reached first.** Read the full KPI report — completed-trade average R, the undetermined all-eligible figure, partial drawdown, by-month breakdown. These are progress checkpoints, matching Stage 1's own discipline (a positive result is a reason for further observation, not validation) — not a claim that the strategy is ready for Stage 3 (bounded practice execution).

No strategy tuning occurs before, during, or as a result of this review cadence — any parameter change would be a new, separately-labelled contract, exactly as Stage 1 required.
