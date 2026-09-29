# Offline strategy comparison — research harness

Isolated research code. Nothing here touches production (`live_scanner.py`, `dashboard_server.py`, `performance_scorer.py`, `alert_lifecycle_log.jsonl`, `alerts.json`), places an order, or changes the public performance display. Read `RESULTS.md` for the actual findings; this file is only "how to run it."

## Reproduce the experiment

```bash
.venv/bin/python3 research/offline_comparison/run_experiment.py
```

Takes under a minute. Reads the frozen dataset already committed at `research/offline_comparison/data/raw/*.csv` (real OANDA bid/ask H4 + M30 candles, 2025-09-30..2026-09-27, fetched 2026-09-29 — see `fetch_research_data.py`'s header for exact provenance) and writes everything to `research/offline_comparison/output/`, which is also committed (the actual ledger/summary/plots this milestone's results are drawn from) — re-running overwrites it deterministically with the same numbers.

## Run the harness correctness checks

```bash
.venv/bin/python3 research/offline_comparison/test_replay_correctness.py
```

Synthetic-fixture checks for timing (entry-window boundary exclusivity), cost accounting (ask-in/bid-out for a long, and vice versa for a short, spread visibly reducing R), ambiguity handling, and position-accounting suppression logic. These establish the *harness* behaves as specified — they are not strategy evidence (see `RESULTS.md`, which uses only the real dataset).

## File map

| File | Role |
|---|---|
| `fetch_research_data.py` | Read-only OANDA fetch (run once, via SSH on the droplet where credentials live — see its header) |
| `data/raw/*.csv` | The frozen dataset this milestone's results are computed from (committed) |
| `data_loader.py` | Loads the CSVs; defines the fixed warm-up/dev/holdout split |
| `configs.py` | The three frozen candidate configurations and the full evaluation contract |
| `strategies/genuine_orb.py` | Candidate 2: real M30-measured opening range |
| `strategies/trend_pullback.py` | Candidate 3: fixed trend-pullback-continuation rule set |
| `replay_scorer.py` | Bid/ask-aware entry/exit state machine (mirrors `src/alert_scorer.py`'s discipline) |
| `replay_engine.py` | Walk-forward loop: signal generation, position policy, lifecycle bookkeeping (reuses `src/alert_lifecycle.py`) |
| `metrics.py` | Aggregation: denominators, avg R, drawdown, by-month/by-pair, unknown-outcome bounds |
| `sensitivity.py` | Cost and execution-delay sensitivity (re-scores existing issued versions, no new signal generation) |
| `run_experiment.py` | The one command above |
| `RESULTS.md` | The actual findings, comparison table, recommendation, roadmap |
| `ISSUES.md` | Consolidated issue list — fixed, deliberate-scope, and deferred |

## To re-fetch the dataset (not needed to reproduce the existing results)

```bash
scp research/offline_comparison/fetch_research_data.py root@<droplet>:/root/fx-signal-model/
ssh root@<droplet> "cd /root/fx-signal-model && set -a && source setup/oanda.env && set +a && .venv/bin/python3 fetch_research_data.py"
scp "root@<droplet>:/root/research_data/*.csv" research/offline_comparison/data/raw/
```

Read-only GET requests to OANDA's `InstrumentsCandles` endpoint (`price=BA`) only — no order is placed, no account state is touched.
