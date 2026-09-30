"""Paper scorer for the prospective baseline: resolves each recorded
decision (observe.py's decisions_log) against the real, timestamped
bid/ask quote-sample stream (quotes_log) for its pair. Same discipline as
research/offline_comparison/replay_scorer.py -- never fabricate a fill,
never use post-deadline information, report uncertainty explicitly -- but
adapted for DISCRETE SAMPLED quotes rather than OHLC candles: a sample is
a single point (no intrabar range), so every crossing is priced at the
actual observed quote, never an idealised exact stop/target level (there
is no "resting order fills exactly at the level" concept when the only
evidence is discrete points).

Fill convention: a long's entry is checked against ASK samples, its exit
against BID samples (and the reverse for a short) -- the same real-spread,
no-double-counting discipline as the offline replay.
"""
from __future__ import annotations
from datetime import datetime, timedelta
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import contract as cfg  # noqa: E402

BASE_CAVEATS = [
    "Prospective PAPER result — a hypothetical decision scored against real, but discretely SAMPLED, "
    "bid/ask quotes. No order was placed; nothing here implies a real fill.",
    "Price movement BETWEEN samples is unobserved and unobservable here — a real, stated limitation "
    "distinct from the offline replay's continuous M30-candle coverage.",
]


def _parse(t: str) -> datetime:
    return datetime.fromisoformat(t)


def _pair_quotes(quotes: list[dict], pair: str) -> list[dict]:
    rows = [q for q in quotes if q.get("pair") == pair and q.get("bid") is not None and q.get("ask") is not None]
    rows.sort(key=lambda q: q["received_at_utc"])
    return rows


def _has_coverage_gap(quotes: list[dict], start: datetime, end: datetime) -> bool:
    """True if consecutive samples within [start, end] are ever more than
    QUOTE_STALENESS_SECONDS apart, OR if there is no sample at all near
    the window's own start/end — a genuine collector/data gap, not merely
    "price never moved into range"."""
    window = [q for q in quotes if start <= _parse(q["received_at_utc"]) <= end]
    if not window:
        return True
    first_gap = (_parse(window[0]["received_at_utc"]) - start).total_seconds()
    if first_gap > cfg.QUOTE_STALENESS_SECONDS:
        return True
    for a, b in zip(window, window[1:]):
        gap = (_parse(b["received_at_utc"]) - _parse(a["received_at_utc"])).total_seconds()
        if gap > cfg.QUOTE_STALENESS_SECONDS:
            return True
    return False


def score_decision(decision: dict, quotes: list[dict], now: datetime) -> dict:
    pair = decision["pair"]
    direction = decision["direction"]
    entry_side, exit_side = ("ask", "bid") if direction == "long" else ("bid", "ask")
    d = 1 if direction == "long" else -1
    stop, target = decision["stop"], decision["target"]
    lo, hi = decision["entry_condition_lo"], decision["entry_condition_hi"]

    published_at = _parse(decision["actual_recording_time_utc"])
    entry_deadline = _parse(decision["entry_expiry_utc"])
    pair_quotes = _pair_quotes(quotes, pair)

    result = {
        "state": None, "assumed_entry_time_utc": None, "assumed_entry_price": None, "same_sample_exit": False,
        "exit_time_utc": None, "exit_price": None, "r_multiple": None,
        "result_type": "prospective_paper", "caveats": list(BASE_CAVEATS),
    }

    entry_time, entry_price, entry_sample = None, None, None
    for q in pair_quotes:
        t = _parse(q["received_at_utc"])
        if t < published_at:
            continue
        if t >= entry_deadline:
            break
        if not q.get("tradeable", True):
            continue
        price = q[entry_side]
        if lo <= price <= hi:
            entry_time, entry_price, entry_sample = t, price, q
            break

    if entry_time is None:
        scan_end = min(entry_deadline, now)
        if _has_coverage_gap(pair_quotes, published_at, scan_end):
            result["state"] = "insufficient_data_entry"
        elif entry_deadline <= now:
            result["state"] = "expired_no_entry"
        else:
            result["state"] = "actionable_open"
        return result

    result["assumed_entry_time_utc"] = entry_time.isoformat()
    result["assumed_entry_price"] = entry_price

    # Same-sample exit: the entry-confirming sample's OWN exit-side price
    # may already show a breach — a real, near-instantaneous fill+exit,
    # not a data artefact. Checked explicitly rather than silently
    # starting the exit scan strictly after this sample.
    exit_price_here = entry_sample[exit_side]
    hit_stop = (exit_price_here <= stop) if d == 1 else (exit_price_here >= stop)
    hit_target = (exit_price_here >= target) if d == 1 else (exit_price_here <= target)
    if hit_stop and hit_target:
        result.update(state="ambiguous_intrabar_exit", same_sample_exit=True)
        return result
    if hit_stop:
        result.update(state="stopped", same_sample_exit=True, exit_time_utc=entry_time.isoformat(), exit_price=exit_price_here,
                      r_multiple=(d * (exit_price_here - entry_price) / abs(entry_price - stop) if entry_price != stop else None))
        return result
    if hit_target:
        result.update(state="targeted", same_sample_exit=True, exit_time_utc=entry_time.isoformat(), exit_price=exit_price_here,
                      r_multiple=(d * (exit_price_here - entry_price) / abs(entry_price - stop) if entry_price != stop else None))
        return result

    risk = abs(entry_price - stop)
    max_exit_time = entry_time + timedelta(hours=decision["max_holding_time_hours"])
    scan_end = min(max_exit_time, now)

    for q in pair_quotes:
        t = _parse(q["received_at_utc"])
        if t <= entry_time:
            continue
        if t > scan_end:
            break
        if not q.get("tradeable", True):
            continue
        price = q[exit_side]
        hs = (price <= stop) if d == 1 else (price >= stop)
        ht = (price >= target) if d == 1 else (price <= target)
        if hs and ht:
            result["state"] = "ambiguous_intrabar_exit"
            return result
        if hs:
            result.update(state="stopped", exit_time_utc=t.isoformat(), exit_price=price,
                          r_multiple=(d * (price - entry_price) / risk if risk else None))
            return result
        if ht:
            result.update(state="targeted", exit_time_utc=t.isoformat(), exit_price=price,
                          r_multiple=(d * (price - entry_price) / risk if risk else None))
            return result

    if max_exit_time <= now:
        # Deadline handling: only a sample within QUOTE_STALENESS_SECONDS
        # of the deadline is fresh enough to use — never bridge a larger
        # gap with a stale earlier quote (mirrors the offline replay's
        # exact-grid-match discipline, adapted for non-grid-aligned
        # continuous sampling: "close enough in wall-clock time" rather
        # than "opens exactly on the deadline").
        candidates = [q for q in pair_quotes if entry_time < _parse(q["received_at_utc"]) <= max_exit_time]
        if not candidates or (max_exit_time - _parse(candidates[-1]["received_at_utc"])).total_seconds() > cfg.QUOTE_STALENESS_SECONDS:
            result["state"] = "incomplete_coverage"
            return result
        last = candidates[-1]
        price = last[exit_side]
        result.update(state="time_exited", exit_time_utc=last["received_at_utc"], exit_price=price,
                      r_multiple=(d * (price - entry_price) / risk if risk else None))
    else:
        result["state"] = "open"
    return result


def score_all(decisions: list[dict], quotes: list[dict], now: datetime) -> list[dict]:
    return [{**dec, **score_decision(dec, quotes, now)} for dec in decisions if dec.get("event_type") == "decision"]
