"""Separate evidence-assisted reconstruction, NOT a replacement v2 score.

No larger gap threshold: a missing interval needs full exit-side historical
candle coverage whose ENTIRE envelope excludes BOTH levels, or continuously
observed explicit non-tradeability. Otherwise the outcome stays unknown.
Never derives a fill from OHLC, guesses ordering, or writes observer records.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

CONTRACT = "supplementary_gap_evidence_v1"
GAP_SECONDS = 15
MAX_CLOSURE_HOURS = 60


def _time(value):
    try:
        t = datetime.fromisoformat(value)
        return t if t.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _valid(q, now):
    received, provider = _time(q.get("received_at_utc")), _time(q.get("oanda_time_utc"))
    try:
        bid, ask = float(q["bid"]), float(q["ask"])
    except (KeyError, ValueError, TypeError):
        return False
    return (received is not None and provider is not None and provider <= received <= now
            and math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask
            and type(q.get("tradeable")) is bool)


def _closure(quotes, start, end):
    """Any day, but ONLY a dense, explicit, valid non-tradeable interval.

    No weekly-calendar inference; silence never establishes closure. This
    expands v2 policy and therefore belongs solely to this separate result.
    """
    if (end - start).total_seconds() > MAX_CLOSURE_HOURS * 3600:
        return False
    previous = start
    found = False
    for q in quotes:
        t = _time(q["received_at_utc"])
        if not start < t < end:
            continue
        if q["tradeable"] is not False or (t - previous).total_seconds() > GAP_SECONDS:
            return False
        previous, found = t, True
    return found and (end - previous).total_seconds() <= GAP_SECONDS


def candle_proof(candles, pair, side, start, end, stop, target, now):
    """Proof of NO level touch, not proof of a fill. Missing bars fail.

    Include encompassing boundary candles, using their entire envelope:
    extra outside-gap movement can reject proof but cannot manufacture it.
    Bars ending after 'now', wrong sides, malformed OHLC, partial bars and
    missing provenance are unusable. A level touch, even in a single bar,
    leaves this gap unknown rather than manufacturing an exit or ordering.
    """
    valid = []
    lower, upper = min(stop, target), max(stop, target)
    for bar in candles:
        if bar.get("pair") != pair or bar.get("side") != side or bar.get("complete") is not True:
            continue
        a, b = _time(bar.get("start_utc")), _time(bar.get("end_utc"))
        fetched = _time(bar.get("fetched_at_utc"))
        if (a is None or b is None or fetched is None or fetched > now or not a < b <= now
                or b <= start or a >= end or not bar.get("evidence_sha256")):
            continue
        try:
            o, h, l, c = (float(bar[key]) for key in ("open", "high", "low", "close"))
        except (KeyError, ValueError, TypeError):
            continue
        if not all(math.isfinite(x) and x > 0 for x in (o, h, l, c)) or not l <= min(o, c) <= max(o, c) <= h:
            continue
        if l <= lower or h >= upper:
            return None  # even a conflicting duplicate must not hide a touch
        valid.append((a, b, l, h, bar["evidence_sha256"]))
    cursor, ids = start, []
    for a, b, low, high, evidence in sorted(valid):
        if b <= cursor:
            continue
        if a > cursor or low <= lower or high >= upper:
            return None
        cursor = b
        ids.append(evidence)
        if cursor >= end:
            return {"kind": "historical_exit_side_envelope_excludes_levels",
                    "from_utc": start.isoformat(), "to_utc": end.isoformat(),
                    "evidence_sha256": sorted(set(ids))}
    return None


def reconstruct(row, quotes, candles, now):
    """Anchor to an existing v2 paper entry; never claims a new entry.

    Inputs are ONE decision window (use QuoteIndex). Derived outcomes stay
    outside live KPI counts. This is retrospectively supplemented evidence,
    not a broker execution record or independent prospective validation.
    """
    result = {"measurement_contract": CONTRACT, "original_state": row.get("state"),
              "as_of_utc": now.isoformat(),
              "decision_id": row.get("decision_id") or row.get("actual_recording_time_utc"),
              "state": "unknown", "exit_time_utc": None, "exit_price": None,
              "r_multiple": None, "proofs": [], "reason": None,
              "result_type": "supplementary_reconstruction_not_live_kpi",
              "unaccounted_costs": ["financing", "commission", "execution slippage"],
              "caveat": "Anchored to the existing assumed entry. Discrete quotes and broker OHLC do not prove real fills or continuous tick-level first touch."}
    entry = _time(row.get("assumed_entry_time_utc"))
    if entry is None or entry > now:
        result["reason"] = "No observable existing entry."
        return result
    deadline = entry + timedelta(hours=float(row["max_holding_time_hours"]))
    price, stop, target = (float(row[k]) for k in ("assumed_entry_price", "stop", "target"))
    long = row["direction"] == "long"
    side, sign = ("bid", 1) if long else ("ask", -1)
    if not (stop < price < target if long else target < price < stop):
        result["reason"] = "Invalid entry/stop/target geometry."
        return result
    raw = sorted((q for q in quotes if q.get("pair") == row["pair"] and _valid(q, now)
                  and entry <= _time(q["received_at_utc"])), key=lambda q: _time(q["received_at_utc"]))
    if not raw or _time(raw[0]["received_at_utc"]) != entry or raw[0]["tradeable"] is not True:
        result["reason"] = "Original entry sample missing."
        return result
    entry_side = "ask" if long else "bid"
    if float(raw[0][entry_side]) != price:
        result["reason"] = "Original entry sample price mismatch."
        return result

    def finish(q, state):
        result.update(state=state, exit_time_utc=q["received_at_utc"], exit_price=float(q[side]),
                      r_multiple=sign * (float(q[side]) - price) / abs(price - stop))
        return result

    previous = entry
    for q in raw:
        if q["tradeable"] is not True:
            continue
        t = _time(q["received_at_utc"])
        if t > deadline + timedelta(hours=MAX_CLOSURE_HOURS):
            break
        gap = (t - previous).total_seconds()
        closed = False
        if gap > GAP_SECONDS:
            closed = _closure(raw, previous, t)
            proof = ({"kind": "continuous_explicit_nontradeability", "from_utc": previous.isoformat(),
                      "to_utc": t.isoformat()} if closed else candle_proof(
                          candles, row["pair"], side, previous, min(t, deadline), stop, target, now))
            if proof is None:
                result.update(reason="Gap lacks sufficient non-touch or non-tradeability evidence.",
                              first_unproved_gap={"from_utc": previous.isoformat(), "to_utc": t.isoformat(), "seconds": gap})
                return result
            result["proofs"].append(proof)
        value = float(q[side])
        # At a delayed closure reopening, stop/target takes precedence. For
        # an ordinary post-deadline sample, never use later movement as a hit.
        if t <= deadline or closed:
            if (value <= stop if long else value >= stop):
                return finish(q, "stopped")
            if (value >= target if long else value <= target):
                return finish(q, "targeted")
        if t >= deadline:
            if closed or (t - deadline).total_seconds() <= GAP_SECONDS:
                if closed:
                    result["deadline_delay_reason"] = "evidenced_nontradeability"
                return finish(q, "time_exited")
            result["reason"] = "No timely deadline quote and no fully evidenced closure."
            return result
        previous = t
    end = min(deadline, now)
    if end > previous and (end - previous).total_seconds() > GAP_SECONDS:
        if _closure(raw, previous, end):
            result.update(state="pending_nontradeable", reason="Awaiting an observed tradeable reopening.")
        else:
            result["reason"] = "Trailing coverage or deadline execution evidence missing."
    elif now < deadline:
        result.update(state="open", reason="Holding deadline not yet reached.")
    else:
        result["reason"] = "No quote at or after the actual deadline."
    return result
