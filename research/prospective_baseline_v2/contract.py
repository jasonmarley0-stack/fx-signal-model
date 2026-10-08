"""Frozen contract for prospective baseline observation v2. See
MEASUREMENT_CONTRACT.md for the reasoning; this file is just the numbers.

Everything about the STRATEGY (pairs, granularity, history window, entry
validity, holding period, entry tolerance) is carried over UNCHANGED from
v1's contract.py -- v2 only changes how samples are validated and how a
deadline during market closure is handled. See MEASUREMENT_CONTRACT.md
section 5 for the explicit "does not change" list.
"""
from __future__ import annotations
from dataclasses import dataclass

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]  # unchanged from v1
DECISION_GRANULARITY = "H4"
ROLLING_HISTORY_BARS = 300

DECISION_POLL_INTERVAL_SECONDS = 300
QUOTE_SAMPLE_INTERVAL_SECONDS = 5

ENTRY_VALIDITY_HOURS = 4.0
MAX_HOLDING_TIME_HOURS = 30.0
ENTRY_TOLERANCE_ATR_MULTIPLE = 0.10

# --- v2 measurement change: see MEASUREMENT_CONTRACT.md section 2 ---
# COVERAGE is measured on RECEIPT time only -- a gap longer than this
# between successful responses is a genuine, unbridged unknown. Same
# numeric value as v1's QUOTE_STALENESS_SECONDS (this is a conflation fix,
# not a threshold increase -- see the contract's explicit "do not inflate
# thresholds" instruction).
COVERAGE_GAP_SECONDS = 15

# Price-creation age (OANDA's own "time" field vs our received_at_utc) is
# reported on every sample as metadata -- see MEASUREMENT_CONTRACT.md
# section 1/2 -- but is NEVER a validity gate in v2. No threshold here
# controls acceptance; this constant does not exist in v2's score.py.
# (v1's QUOTE_MAX_PROVIDER_AGE_SECONDS is intentionally not carried over.)

# --- v2 collection-reliability additions: see MEASUREMENT_CONTRACT.md section 2 ---
REQUEST_TIMEOUT_SECONDS = 8.0          # per HTTP attempt
MAX_RETRY_ATTEMPTS = 2                  # additional attempts after the first, for retryable errors only
RETRY_BACKOFF_BASE_SECONDS = 1.0        # exponential backoff: 1s, 2s, ...
MAX_TOTAL_REQUEST_SECONDS = 20.0        # hard ceiling across all attempts -- one request can never stall collection
                                         # for minutes, bounding the exact failure mode diagnosed in the v1 run
                                         # (a single stalled request blocked the whole observer loop for 334s)

# --- v2 market-closure detection: see MEASUREMENT_CONTRACT.md section 4 ---
# Reference only, cross-checked against empirical tradeable=False evidence
# in the actual recorded stream -- never the sole basis for treating a gap
# as closure. OANDA FX trading: Sun ~21:00-22:00 UTC -> Fri ~21:00-22:00
# UTC (varies with US/UK daylight saving); a +/- margin absorbs that
# uncertainty rather than hardcoding an exact minute.
WEEKLY_CLOSURE_FRIDAY_UTC_HOUR = 20   # from this hour Friday...
WEEKLY_CLOSURE_SUNDAY_UTC_HOUR = 23   # ...until this hour Sunday, UTC, is the closure-detection candidate window

# How late an actual reopen sample may arrive, relative to the EXPECTED
# reopen point (next Sunday at WEEKLY_CLOSURE_SUNDAY_UTC_HOUR), and still
# be accepted as explaining a gap. This is the tight bound: one genuine
# tradeable=False observation near the start of a long silence must not
# excuse silence of arbitrary length -- only up to this margin past when
# the market should actually have reopened. A margin, not zero, because
# WEEKLY_CLOSURE_SUNDAY_UTC_HOUR is itself an approximation (DST, a few
# minutes either side of the venue's real reopen).
MAX_REOPEN_DELAY_FROM_EXPECTED_HOURS = 6.0

# Outer, absolute sanity ceiling (independent of the expected-reopen
# estimate above) and also the publisher's bounded quote-window margin
# for v2 (see research_snapshot_publisher.py::_closure_margin_seconds) --
# retains far more room than MAX_REOPEN_DELAY_FROM_EXPECTED_HOURS ever
# needs, so the publisher's memory-bounded quote window can never
# truncate the evidence score.py's tighter acceptance check is actually
# willing to use.
MAX_CLOSURE_DEADLINE_DELAY_HOURS = 60


@dataclass
class ObservationRun:
    """Unchanged shape from v1 -- documents the run, not enforced in code."""
    run_id: str
    started_at_utc: str
    contract_version: str = "v2"
