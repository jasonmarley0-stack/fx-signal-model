"""Bid/ask-aware entry/exit scorer for the offline replay. Mirrors
src/alert_scorer.py's state machine (conservative entry confirmation via
a candle's own OPEN, [start,end) exclusive windows, intrabar stop/target
ambiguity, coverage-checked time exits) -- the same discipline, ported to
consume real bid/ask M30 series instead of a single midpoint series, since
that discipline is exactly what this milestone's cost accounting needs.
Kept as a separate, purpose-built module in this isolated research
harness rather than bolted onto src/alert_scorer.py, which is shared with
the production alert-lifecycle branch and out of scope for this task.

Fill convention (see configs.py FILL_SIDE): a long's entry is checked
against the ASK series and its exit against the BID series; a short is
the mirror. This charges the real spread exactly once, in the gap between
entry and exit price -- no separate spread deduction is applied anywhere
else, so cost is never double-counted.
"""
from __future__ import annotations
from datetime import datetime, timedelta
import pandas as pd

BASE_CAVEATS = [
    "Replay result — a hypothetical alert scored against historical OANDA bid/ask candles. "
    "No historical subscriber received this alert; nothing here implies they did.",
    "Entry is an assumption for scoring purposes: a fill is only confirmed when a candle's own "
    "open lands inside the entry condition, on the correct (ask for long / bid for short) side.",
]


def _candle_interval(candles: pd.DataFrame) -> timedelta:
    if len(candles.index) >= 2:
        return candles.index[1] - candles.index[0]
    return timedelta(minutes=30)


def _find_entry(series: pd.DataFrame, start: datetime, end: datetime, lo: float, hi: float):
    window = series[(series.index >= pd.Timestamp(start)) & (series.index < pd.Timestamp(end))]
    touched = False
    for ts, bar in window.iterrows():
        if bar["low"] <= hi and bar["high"] >= lo:
            touched = True
            if lo <= bar["open"] <= hi:
                return ts.to_pydatetime(), float(bar["open"]), "confirmed"
    return None, None, ("touched" if touched else "never_reached")


def score_replay_version(version: dict, effective_entry_window_end: datetime, effective_end_reason: str,
                          m30_bid: pd.DataFrame, m30_ask: pd.DataFrame, now: datetime) -> dict:
    """`version` carries: pair, direction, entry, entry_condition_lo/hi,
    stop, target, published_at (datetime), max_holding_time_hours."""
    direction = version["direction"]
    entry_series = m30_ask if direction == "long" else m30_bid
    exit_series = m30_bid if direction == "long" else m30_ask
    published_at = version["published_at"]

    result = {
        "state": None, "assumed_entry_time_utc": None, "assumed_entry_price": None,
        "exit_time_utc": None, "exit_price": None, "r_multiple": None,
        "result_type": "hypothetical_replay", "caveats": list(BASE_CAVEATS),
    }

    if effective_end_reason == "cancelled" and effective_entry_window_end <= published_at:
        result["state"] = "cancelled"
        return result

    entry_time, entry_price, entry_status = _find_entry(
        entry_series, published_at, effective_entry_window_end, version["entry_condition_lo"], version["entry_condition_hi"])

    if entry_time is None:
        if entry_status == "touched":
            result["state"] = "insufficient_data_entry"
            return result
        if effective_end_reason == "cancelled":
            result["state"] = "cancelled"
        elif effective_entry_window_end <= now:
            result["state"] = "expired_no_entry"
        else:
            result["state"] = "actionable_open"
        return result

    result["assumed_entry_time_utc"] = entry_time.isoformat()
    result["assumed_entry_price"] = entry_price

    d = 1 if direction == "long" else -1
    stop, target = version["stop"], version["target"]
    risk = abs(entry_price - stop)
    max_exit_time = entry_time + timedelta(hours=version["max_holding_time_hours"])
    interval = _candle_interval(exit_series)
    scan_end = min(max_exit_time, now)

    # >= entry_time, NOT > — the entry candle itself must be checked too
    # (entry happens at that candle's open, so its own high/low afterward
    # can still reach stop/target before the candle closes).
    #
    # Only FULLY COMPLETED candles (open + interval <= scan_end) may
    # contribute a stop/target/ambiguity hit via their high/low. A candle
    # whose own open is at-or-before scan_end but whose span extends PAST
    # it straddles the boundary — its high/low reflect price action that
    # partly occurs after the deadline being evaluated, so using them
    # could manufacture a stop/target hit (or ambiguity) that might not
    # actually have happened before that deadline. This is a distinct bug
    # from the entry-candle-skip fix above: that one was about which
    # candles are INCLUDED; this one is about which INCLUDED candles may
    # use their full high/low versus only their own opening quote.
    completed_post_entry = exit_series[(exit_series.index >= pd.Timestamp(entry_time)) &
                                        (exit_series.index + interval <= pd.Timestamp(scan_end))]
    for ts, bar in completed_post_entry.iterrows():
        hit_stop = (bar["low"] <= stop) if d == 1 else (bar["high"] >= stop)
        hit_target = (bar["high"] >= target) if d == 1 else (bar["low"] <= target)
        if hit_stop and hit_target:
            result["state"] = "ambiguous_intrabar_exit"
            return result
        if hit_stop:
            # Gap handling: if the bar's own OPEN has already crossed the
            # stop, the first available quote in this bar was already
            # through it — pricing the exit exactly at the stop level
            # would assume a fill that was never actually quoted. Use the
            # bar's open (the first real observed price past the level)
            # instead; only price exactly at the stop when the level was
            # reached via the bar's high/low, not gapped through at open.
            gapped = (bar["open"] <= stop) if d == 1 else (bar["open"] >= stop)
            exit_price = float(bar["open"]) if gapped else stop
            result.update(state="stopped", exit_time_utc=ts.to_pydatetime().isoformat(), exit_price=exit_price,
                          r_multiple=(d * (exit_price - entry_price) / risk if risk else None))
            return result
        if hit_target:
            gapped = (bar["open"] >= target) if d == 1 else (bar["open"] <= target)
            exit_price = float(bar["open"]) if gapped else target
            result.update(state="targeted", exit_time_utc=ts.to_pydatetime().isoformat(), exit_price=exit_price,
                          r_multiple=(d * (exit_price - entry_price) / risk if risk else None))
            return result

    if max_exit_time <= now:
        # No fully-completed candle produced a hit. At the boundary itself,
        # only a candle whose OWN OPEN falls EXACTLY at max_exit_time is a
        # real observed quote AT the deadline — this system's entries are
        # always confirmed on an M30 candle's own open (grid-aligned), and
        # this contract's max_holding_time_hours is always a whole multiple
        # of the M30 interval, so an exact-match candle exists whenever the
        # data isn't gapped there. A candle that merely SPANS the deadline
        # without opening exactly on it is not a fresher observation than
        # its own (earlier) open already was, and must not be fabricated
        # into a deadline fill — reported as incomplete_coverage instead.
        boundary_candidates = exit_series[exit_series.index == pd.Timestamp(max_exit_time)]
        if boundary_candidates.empty:
            result["state"] = "incomplete_coverage"
            return result
        ts = boundary_candidates.index[-1]
        bar = boundary_candidates.iloc[-1]
        hit_stop_at_open = (bar["open"] <= stop) if d == 1 else (bar["open"] >= stop)
        hit_target_at_open = (bar["open"] >= target) if d == 1 else (bar["open"] <= target)
        if hit_stop_at_open and hit_target_at_open:
            result["state"] = "ambiguous_intrabar_exit"
            return result
        if hit_stop_at_open:
            result.update(state="stopped", exit_time_utc=ts.to_pydatetime().isoformat(), exit_price=float(bar["open"]),
                          r_multiple=(d * (float(bar["open"]) - entry_price) / risk if risk else None))
            return result
        if hit_target_at_open:
            result.update(state="targeted", exit_time_utc=ts.to_pydatetime().isoformat(), exit_price=float(bar["open"]),
                          r_multiple=(d * (float(bar["open"]) - entry_price) / risk if risk else None))
            return result
        exit_price = float(bar["open"])
        result.update(state="time_exited", exit_time_utc=max_exit_time.isoformat(), exit_price=exit_price,
                      r_multiple=(d * (exit_price - entry_price) / risk if risk else None))
    else:
        result["state"] = "open"
    return result
