"""The prospective-observation contract for Stage 2 (baseline only) --
frozen and documented here, separately from research/offline_comparison/
configs.py's replay contract, per the explicit instruction not to blend
the two. Read this alongside that file's own contract section before
comparing any numbers across the two.

Strategy: the SAME frozen baseline as Stage 1 -- src/strategies/
composite.py's technical_score() and src/combiner.py's combine_signal(),
called directly (not reimplemented), alpha=1.0/pestle_score=0.0 matching
live_scanner.py's actual deployed configuration. Not reused: genuine_orb,
trend_pullback (Stage 1 already rejected both as tested).

## What is different here from the offline replay, and why

| Aspect | Offline replay (Stage 1) | Prospective observation (Stage 2) |
|---|---|---|
| History for indicators | Expanding, from the full frozen dataset | ROLLING 300 H4 bars, re-fetched fresh each decision tick -- matches live_scanner.py's own HISTORY_BARS=300, not the offline replay's full-history approach |
| Decision latency | Zero (decision_time = candle completion, exactly) | REAL, measured: the gap between a candle's actual completion and this process's own poll noticing and computing it (bounded by DECISION_POLL_INTERVAL_SECONDS, never assumed zero) |
| Outcome resolution | M30 candles (open/high/low/close) | Discrete SAMPLED quotes (bid/ask only, no intrabar range) every QUOTE_SAMPLE_INTERVAL_SECONDS -- price movement BETWEEN samples is unobserved and unobservable here, a real, stated limitation the M30-candle approach did not have |
| Deadline execution delay | 0 (EXECUTION_DELAY_MINUTES in the replay contract) | Bounded by QUOTE_SAMPLE_INTERVAL_SECONDS -- the earliest a deadline-boundary quote can be observed is the next sample after it, not the instant itself |
| Entry/exit cost basis | Real historical bid/ask candles | Real, live-observed bid/ask quotes at sample time -- genuinely observed, not reconstructed |

None of these values are silently introduced as new thresholds layered on top of the offline contract -- they are this stage's OWN contract, stated here once, applied consistently, and never blended into Stage 1's numbers or vice versa.

## Frozen constants (do not change without a new, separately-labelled contract)
"""
from __future__ import annotations
from dataclasses import dataclass

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]  # same 7 majors as the live scanner
DECISION_GRANULARITY = "H4"
ROLLING_HISTORY_BARS = 300  # matches live_scanner.py's HISTORY_BARS exactly -- a rolling window, not expanding history

DECISION_POLL_INTERVAL_SECONDS = 300  # 5 minutes -- how often the collector checks for a newly-completed H4 candle
QUOTE_SAMPLE_INTERVAL_SECONDS = 5     # how often real bid/ask is sampled and recorded

ENTRY_VALIDITY_HOURS = 4.0            # same value as the offline replay contract, for comparability -- not re-derived here
MAX_HOLDING_TIME_HOURS = 30.0         # same value as the offline replay contract and performance_scorer.py's MAX_LOOKAHEAD_HOURS
ENTRY_TOLERANCE_ATR_MULTIPLE = 0.10   # same as the offline replay contract

# A quote sample is "stale" (not usable to confirm entry/exit/deadline)
# once it is older than this relative to the moment being evaluated --
# bounds how far back a real quote may be trusted to stand in for "now".
QUOTE_STALENESS_SECONDS = 15  # 3x the sample interval; a genuine gap beyond this is recorded as a coverage gap, not silently bridged


@dataclass(frozen=True)
class ObservationRun:
    """Identifies one observation run/session -- allows multiple runs to
    coexist in the same log directory without silently blending, and lets
    a restarted process resume the SAME run rather than starting a new
    one, via run_id continuity in the persisted state file."""
    run_id: str
    started_at_utc: str
    scanner_version: str = "PROSPECTIVE_H4_majors_baseline_v1"
