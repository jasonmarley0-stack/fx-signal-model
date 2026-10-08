"""v2 paper scorer. See MEASUREMENT_CONTRACT.md -- this module implements
it. Same discipline as v1 (never fabricate a fill, never use post-deadline
information except the one labeled closure exception in section 4, report
uncertainty explicitly) with the coverage/validity conflation fixed:
price-creation age (OANDA's own clock) is reported but never a validity
gate; coverage is measured on receipt-time continuity alone.

Everything NOT about measurement is unchanged from v1: ask-in/bid-out
sides, entry window, original stop/target/entry-condition levels,
chronological one-position-per-pair suppression, the 30-hour holding rule
as a real elapsed-time bound.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime, timedelta
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import contract as cfg  # noqa: E402

BASE_CAVEATS = [
    "Prospective PAPER result (v2 measurement contract) — a hypothetical decision scored against real, "
    "discretely SAMPLED, bid/ask quotes. No order was placed; nothing here implies a real fill.",
    "Price movement BETWEEN samples is unobserved and unobservable here. A reported crossing is an OBSERVED "
    "crossing at the receipt time of the sample that showed it — not a claim that it was the first instant "
    "the level was crossed; the true path between 5-second polls is unknown.",
]


def _parse(t: str) -> datetime:
    return datetime.fromisoformat(t)


def _try_parse(t) -> datetime | None:
    if not t:
        return None
    try:
        return datetime.fromisoformat(t)
    except (ValueError, TypeError):
        return None


def _valid_price(p) -> float | None:
    try:
        f = float(p)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f


def _valid_pair_quotes(quotes: list[dict], pair: str, now: datetime) -> list[dict]:
    """v2: COVERAGE is receipt-time-only (MEASUREMENT_CONTRACT.md section
    2) -- the provider's price-creation AGE is never a gate, so an old-but-
    unchanged, successfully-returned price is usable. That is not the same
    as skipping provider-timestamp validity entirely: the field itself
    must still be present, parseable, and not claim to be from the future
    relative to receipt -- a missing/malformed/future-dated provider
    timestamp is a malformed observation, not a legitimate "unchanged
    older price" case, and is rejected here just like a crossed or
    non-finite price. The distinction v2 draws is AGE vs VALIDITY: any
    non-negative age is accepted; no provider timestamp at all, or one
    that doesn't parse, or one that's impossibly after our own receipt
    time, is not."""
    out = []
    for q in quotes:
        if q.get("pair") != pair:
            continue
        bid, ask = _valid_price(q.get("bid")), _valid_price(q.get("ask"))
        if bid is None or ask is None or bid > ask:
            continue
        if not q.get("tradeable", False):
            continue
        recv = _try_parse(q.get("received_at_utc"))
        if recv is None or recv > now:
            continue
        oanda_t = _try_parse(q.get("oanda_time_utc"))
        if oanda_t is None:
            continue  # missing/malformed provider timestamp -> not a usable observation
        provider_age = (recv - oanda_t).total_seconds()
        if provider_age < 0:
            continue  # a provider timestamp "from the future" relative to our own receipt is corrupt data, not an old price
        out.append({**q, "_recv": recv, "bid": bid, "ask": ask, "_provider_age_seconds": provider_age})
    out.sort(key=lambda q: q["_recv"])
    return out


def _has_coverage_gap(valid_quotes: list[dict], start: datetime, end: datetime) -> bool:
    """Unchanged logic from v1 (leading, mid-window, trailing gaps all
    checked) — only the INPUT pool changed (no provider-age exclusions).
    A stretch longer than COVERAGE_GAP_SECONDS with no successful receipt
    anywhere in [start, end] is a genuine, unbridged unknown."""
    window = [q for q in valid_quotes if start <= q["_recv"] <= end]
    if not window:
        return True
    if (window[0]["_recv"] - start).total_seconds() > cfg.COVERAGE_GAP_SECONDS:
        return True
    for a, b in zip(window, window[1:]):
        if (b["_recv"] - a["_recv"]).total_seconds() > cfg.COVERAGE_GAP_SECONDS:
            return True
    if (end - window[-1]["_recv"]).total_seconds() > cfg.COVERAGE_GAP_SECONDS:
        return True
    return False


def _in_closure_reference_window(t: datetime) -> bool:
    """Weekday/hour reference only — see MEASUREMENT_CONTRACT.md section 4.
    Never the sole basis for a closure classification; always combined
    with empirical tradeable evidence in _detect_closure."""
    wd = t.weekday()  # Mon=0 .. Sun=6
    if wd == 4 and t.hour >= cfg.WEEKLY_CLOSURE_FRIDAY_UTC_HOUR:  # Friday evening
        return True
    if wd == 5:  # all Saturday
        return True
    if wd == 6 and t.hour < cfg.WEEKLY_CLOSURE_SUNDAY_UTC_HOUR:  # Sunday before reopen
        return True
    return False


def _valid_nontradeable_receipts(raw_pair_quotes: list[dict], start: datetime, end: datetime) -> list[datetime]:
    """Receipt times of well-formed raw observations in [start, end] where
    OANDA explicitly marked `tradeable` False, sorted. Three distinct
    failure modes are excluded, all by design:
    - A quote with the `tradeable` key simply ABSENT is not evidence of
      anything -- `.get("tradeable", False)` would silently conflate "the
      provider told us the market is closed" with "we don't know what the
      provider said"; only `is False` (the key present AND exactly False)
      counts.
    - A malformed observation (non-finite/crossed bid-ask, or an
      unparseable provider timestamp) happening to carry tradeable=False
      is not a trustworthy observation of anything, including closure.
    - Receipts outside [start, end] are irrelevant to explaining THIS gap."""
    out = []
    for q in raw_pair_quotes:
        recv = _try_parse(q.get("received_at_utc"))
        if recv is None or not (start <= recv <= end):
            continue
        if q.get("tradeable") is not False:
            continue
        bid, ask = _valid_price(q.get("bid")), _valid_price(q.get("ask"))
        if bid is None or ask is None or bid > ask:
            continue
        if _try_parse(q.get("oanda_time_utc")) is None:
            continue
        out.append(recv)
    out.sort()
    return out


def _closure_fully_evidenced(raw_pair_quotes: list[dict], start: datetime, end: datetime) -> bool:
    """The interval [start, end] is explained by DEMONSTRATED closure only
    when there is CONTINUOUS coverage of explicit tradeable=False
    observations across the whole interval -- no receipt-time gap wider
    than COVERAGE_GAP_SECONDS from `start`, between consecutive
    non-tradeable receipts, or up to `end` -- exactly the same continuity
    discipline _has_coverage_gap already applies to ordinary (tradeable)
    coverage. During a genuine closure the provider keeps responding at
    the normal poll cadence, just marked non-tradeable; real closure
    looks like dense evidence throughout, not one data point. This
    deliberately does NOT reason from proximity to an ESTIMATED reopen
    time (removed per correction order: "remove the assumption that a
    reopening sample arriving within six hours of an approximate Sunday
    time proves closure throughout the missing interval") -- a lone
    early observation followed by silence is indistinguishable from an
    ordinary, unexplained collection outage and must not be treated as
    explaining an arbitrarily long subsequent gap."""
    receipts = _valid_nontradeable_receipts(raw_pair_quotes, start, end)
    if not receipts:
        return False
    if (receipts[0] - start).total_seconds() > cfg.COVERAGE_GAP_SECONDS:
        return False
    for a, b in zip(receipts, receipts[1:]):
        if (b - a).total_seconds() > cfg.COVERAGE_GAP_SECONDS:
            return False
    if (end - receipts[-1]).total_seconds() > cfg.COVERAGE_GAP_SECONDS:
        return False
    return True


def _closure_explains_gap(raw_pair_quotes: list[dict], gap_start: datetime, gap_end: datetime) -> bool:
    """Used by the exit scan to decide whether a MID-WINDOW gap (the
    market can close before the holding deadline, not only exactly at
    it) may be bridged rather than disqualified -- reference-window
    timing AND continuous closure evidence across the full gap are both
    required."""
    if not (_in_closure_reference_window(gap_start) or _in_closure_reference_window(gap_end)):
        return False
    return _closure_fully_evidenced(raw_pair_quotes, gap_start, gap_end)


def _next_reopen_estimate(t: datetime) -> datetime:
    """Conservative estimate of the next weekly reopen point (Sunday at
    WEEKLY_CLOSURE_SUNDAY_UTC_HOUR, on or after `t`) -- used ONLY to keep
    a still-unresolved, plausibly-closure-affected position conservatively
    occupying its pair for suppression purposes (see build_paper_ledger).
    Never used to resolve the trade's own state, which stays honestly
    incomplete_coverage until actual evidence confirms resolution."""
    candidate = t.replace(hour=cfg.WEEKLY_CLOSURE_SUNDAY_UTC_HOUR, minute=0, second=0, microsecond=0)
    while candidate.weekday() != 6 or candidate < t:
        candidate += timedelta(days=1)
    return candidate


def _detect_closure(raw_pair_quotes: list[dict], deadline: datetime, now: datetime) -> dict | None:
    """Demonstrated, not assumed (MEASUREMENT_CONTRACT.md section 4): only
    returns a closure finding when (a) the deadline falls inside the
    weekly closure reference window, (b) a tradeable=True reopen sample
    is actually found within the outer MAX_CLOSURE_DEADLINE_DELAY_HOURS
    search bound (a sanity/performance ceiling on the SCAN only, not an
    acceptance criterion), and (c) the ENTIRE interval between the
    deadline and that reopen sample is CONTINUOUSLY evidenced by
    tradeable=False observations (_closure_fully_evidenced) -- not merely
    "some evidence exists somewhere in the window". Returns None (no
    closure finding — stay incomplete_coverage) if any of these is
    missing, e.g. pure absence with no tradeable=False evidence at all
    ("weekend absence alone must remain unexplained"), a tradeable=True
    quote appears too early to be consistent with the claimed closure, or
    the evidence is sparse/discontinuous (a real observation followed by
    unexplained silence before the reopen-shaped sample).

    Returns {"reopen_quote": <first valid quote after the closed stretch>}
    or None.
    """
    if not _in_closure_reference_window(deadline):
        return None
    scan_end = min(deadline + timedelta(hours=cfg.MAX_CLOSURE_DEADLINE_DELAY_HOURS), now)
    window = [q for q in raw_pair_quotes if deadline <= (_try_parse(q.get("received_at_utc")) or deadline - timedelta(seconds=1)) <= scan_end]
    reopen_quote = None
    for q in sorted(window, key=lambda q: q["received_at_utc"]):
        if q.get("tradeable") is True:
            bid, ask = _valid_price(q.get("bid")), _valid_price(q.get("ask"))
            if bid is not None and ask is not None and bid <= ask and _try_parse(q.get("oanda_time_utc")) is not None:
                recv = _try_parse(q.get("received_at_utc"))
                reopen_quote = {**q, "_recv": recv}
                break
        # a non-tradeable (or malformed) quote during the candidate window is
        # exactly what closure predicts -- keep scanning for reopen.
    if reopen_quote is None:
        return None
    if not _closure_fully_evidenced(raw_pair_quotes, deadline, reopen_quote["_recv"]):
        return None  # sparse/discontinuous evidence is NOT, by itself, demonstrated closure throughout the gap
    return {"reopen_quote": reopen_quote}


def score_decision(decision: dict, quotes: list[dict], now: datetime) -> dict:
    pair = decision["pair"]
    direction = decision["direction"]
    entry_side, exit_side = ("ask", "bid") if direction == "long" else ("bid", "ask")
    d = 1 if direction == "long" else -1
    stop, target = decision["stop"], decision["target"]
    lo, hi = decision["entry_condition_lo"], decision["entry_condition_hi"]

    published_at = _parse(decision["actual_recording_time_utc"])
    entry_deadline = _parse(decision["entry_expiry_utc"])
    vq = _valid_pair_quotes(quotes, pair, now)
    raw_pair_quotes = [q for q in quotes if q.get("pair") == pair]

    result = {
        "state": None, "assumed_entry_time_utc": None, "assumed_entry_price": None, "same_sample_exit": False,
        "crossing_type": None, "exit_time_utc": None, "exit_price": None, "r_multiple": None,
        "scheduled_exit_time_utc": None, "execution_delay_seconds": None, "deadline_delay_reason": None,
        "assumed_occupied_until_utc": None,
        "result_type": "prospective_paper_v2", "contract_version": "v2", "caveats": list(BASE_CAVEATS),
    }

    # --- Entry scan ---
    entry_time, entry_price, entry_sample = None, None, None
    for q in vq:
        t = q["_recv"]
        if t < published_at:
            continue
        if t >= entry_deadline:
            break
        price = q[entry_side]
        if lo <= price <= hi:
            entry_time, entry_price, entry_sample = t, price, q
            break

    if entry_time is None:
        scan_end = min(entry_deadline, now)
        if _has_coverage_gap(vq, published_at, scan_end):
            result["state"] = "insufficient_data_entry"
        elif entry_deadline <= now:
            result["state"] = "expired_no_entry"
        else:
            result["state"] = "actionable_open"
        return result

    result["assumed_entry_time_utc"] = entry_time.isoformat()
    result["assumed_entry_price"] = entry_price

    # --- Same-sample exit ---
    exit_price_here = entry_sample[exit_side]
    hit_stop = (exit_price_here <= stop) if d == 1 else (exit_price_here >= stop)
    hit_target = (exit_price_here >= target) if d == 1 else (exit_price_here <= target)
    if hit_stop and hit_target:
        result.update(state="ambiguous_intrabar_exit", same_sample_exit=True)
        return result
    if hit_stop or hit_target:
        state = "stopped" if hit_stop else "targeted"
        result.update(state=state, same_sample_exit=True, crossing_type="observed",
                      exit_time_utc=entry_time.isoformat(), exit_price=exit_price_here,
                      r_multiple=(d * (exit_price_here - entry_price) / abs(entry_price - stop) if entry_price != stop else None))
        return result

    risk = abs(entry_price - stop)
    max_exit_time = entry_time + timedelta(hours=decision["max_holding_time_hours"])
    hit_scan_end = min(max_exit_time, now)

    # --- Gap-aware exit scan ---
    # A gap is disqualifying UNLESS it is explained by DEMONSTRATED market
    # closure (MEASUREMENT_CONTRACT.md section 4) -- the market can close
    # BEFORE the holding deadline, not only exactly at it, so this check
    # happens on every gap encountered during the scan, not only at the
    # end. A bridged gap falls through to the SAME stop/target check as
    # every other sample immediately below, giving stop/target explicit
    # precedence over anything closure-related, even at the very first
    # post-reopen sample.
    last_checked = entry_time
    for q in vq:
        t = q["_recv"]
        if t <= entry_time:
            continue
        if t > hit_scan_end:
            break
        gap_seconds = (t - last_checked).total_seconds()
        if gap_seconds > cfg.COVERAGE_GAP_SECONDS:
            if _closure_explains_gap(raw_pair_quotes, last_checked, t):
                result["caveats"] = result["caveats"] + [
                    f"A coverage gap from {last_checked.isoformat()} to {t.isoformat()} ({gap_seconds:.0f}s) is "
                    "explained by demonstrated market closure (a tradeable=false observation in that interval) and "
                    "was bridged, not treated as a disqualifying unknown. Price action DURING the closure itself "
                    "remains unobserved; only the sample at/after reopening is used."]
            else:
                result["state"] = "incomplete_coverage"
                if _in_closure_reference_window(last_checked) or _in_closure_reference_window(t):
                    # plausibly closure-timed but not yet demonstrated (e.g. no
                    # affirmative evidence recorded, or reopen not yet observed) --
                    # stay honestly unknown, but conservatively preserve pair
                    # occupancy through the next plausible reopen for suppression.
                    result["assumed_occupied_until_utc"] = _next_reopen_estimate(t).isoformat()
                return result
        price = q[exit_side]
        hs = (price <= stop) if d == 1 else (price >= stop)
        ht = (price >= target) if d == 1 else (price <= target)
        if hs and ht:
            result["state"] = "ambiguous_intrabar_exit"
            return result
        if hs or ht:
            state = "stopped" if hs else "targeted"
            result.update(state=state, crossing_type="observed", exit_time_utc=t.isoformat(), exit_price=price,
                          r_multiple=(d * (price - entry_price) / risk if risk else None))
            return result
        last_checked = t

    if max_exit_time <= now:
        if _has_coverage_gap(vq, last_checked, max_exit_time):
            if _closure_explains_gap(raw_pair_quotes, last_checked, max_exit_time):
                pass  # preceding coverage requirement satisfied by demonstrated closure -- fall through to deadline handling below
            else:
                result["state"] = "incomplete_coverage"
                if _in_closure_reference_window(last_checked) or _in_closure_reference_window(max_exit_time):
                    result["assumed_occupied_until_utc"] = _next_reopen_estimate(max_exit_time).isoformat()
                return result
        post_deadline = [q for q in vq if q["_recv"] >= max_exit_time]
        if post_deadline and (post_deadline[0]["_recv"] - max_exit_time).total_seconds() <= cfg.COVERAGE_GAP_SECONDS:
            exit_sample = post_deadline[0]
            exit_time = exit_sample["_recv"]
            exit_price = exit_sample[exit_side]
            result.update(
                state="time_exited", crossing_type="observed", exit_time_utc=exit_time.isoformat(), exit_price=exit_price,
                scheduled_exit_time_utc=max_exit_time.isoformat(),
                execution_delay_seconds=(exit_time - max_exit_time).total_seconds(),
                r_multiple=(d * (exit_price - entry_price) / risk if risk else None),
            )
            return result
        # No ordinary post-deadline sample within the normal coverage-gap
        # bound -- the ONE new v2 policy (MEASUREMENT_CONTRACT.md section 4)
        # applies only when closure is demonstrated, not assumed.
        closure = _detect_closure(raw_pair_quotes, max_exit_time, now)
        if closure is not None:
            exit_sample = closure["reopen_quote"]
            exit_time = exit_sample["_recv"]
            exit_price = exit_sample[exit_side]
            # Explicit precedence at reopening: stop/target is checked on the
            # reopen sample BEFORE it is accepted as a scheduled time-exit --
            # the market could easily have gapped through either level over a
            # closed weekend, and that observed crossing takes priority over
            # treating the sample as "just" the deadline's delayed execution.
            reopen_price = exit_sample[exit_side]
            hs = (reopen_price <= stop) if d == 1 else (reopen_price >= stop)
            ht = (reopen_price >= target) if d == 1 else (reopen_price <= target)
            if hs and ht:
                result["state"] = "ambiguous_intrabar_exit"
                return result
            if hs or ht:
                state = "stopped" if hs else "targeted"
                result.update(state=state, crossing_type="observed", exit_time_utc=exit_time.isoformat(), exit_price=reopen_price,
                              r_multiple=(d * (reopen_price - entry_price) / risk if risk else None))
                result["caveats"] = result["caveats"] + [
                    f"Stop/target was already crossed at the first tradeable sample after a demonstrated market-"
                    "closure window -- reported as the crossing, not as the scheduled deadline exit. Stop/target "
                    "takes precedence over a deadline exit at reopening, in all cases."]
                return result
            result.update(
                state="time_exited", crossing_type="observed", exit_time_utc=exit_time.isoformat(), exit_price=exit_price,
                scheduled_exit_time_utc=max_exit_time.isoformat(),
                execution_delay_seconds=(exit_time - max_exit_time).total_seconds(),
                deadline_delay_reason="market_closure",
                r_multiple=(d * (exit_price - entry_price) / risk if risk else None),
            )
            result["caveats"] = result["caveats"] + [
                "Deadline execution delayed past the nominal 30-hour boundary: the deadline fell within a "
                "demonstrated market-closure window (empirical tradeable=false evidence), and no fill "
                "was possible against a closed market in a real account either. Executed at the first tradeable "
                "sample after reopening (stop/target checked first — see caveats). This is a new, explicitly "
                "labeled v2 exit policy — see MEASUREMENT_CONTRACT.md section 4 — never silently substituted "
                "into a v1-contract comparison."]
            return result
        result["state"] = "incomplete_coverage"
        if _in_closure_reference_window(max_exit_time):
            # Deadline is plausibly inside a closure that simply hasn't been
            # confirmed yet (reopen not observed within the data available
            # so far) -- stay honestly unknown, but preserve occupancy.
            result["assumed_occupied_until_utc"] = _next_reopen_estimate(max_exit_time).isoformat()
        return result
    else:
        # --- Trailing coverage check for a still-open trade ---
        # The deadline hasn't arrived yet, but a long silence since the
        # last checked sample is itself a real, present-tense coverage
        # problem -- report it immediately rather than silently saying
        # "open" (as if everything is fine) until the deadline eventually
        # forces the question. Bridged exactly like any other gap if
        # closure demonstrably explains it; otherwise honestly unknown.
        trailing_gap = (now - last_checked).total_seconds()
        if trailing_gap > cfg.COVERAGE_GAP_SECONDS:
            if _closure_explains_gap(raw_pair_quotes, last_checked, now):
                result["state"] = "open"
                result["caveats"] = result["caveats"] + [
                    f"A trailing coverage gap since {last_checked.isoformat()} is explained by demonstrated "
                    "market closure and does not currently disqualify this still-open position."]
            else:
                result["state"] = "incomplete_coverage"
                if _in_closure_reference_window(last_checked) or _in_closure_reference_window(now):
                    result["assumed_occupied_until_utc"] = _next_reopen_estimate(now).isoformat()
        else:
            result["state"] = "open"
    return result


def score_all(decisions: list[dict], quotes: list[dict], now: datetime) -> list[dict]:
    return [{**dec, **score_decision(dec, quotes, now)} for dec in decisions if dec.get("event_type") == "decision"]


def build_paper_ledger(decisions: list[dict], quotes: list[dict], now: datetime) -> list[dict]:
    """Pair-local chronological one-entered-position-per-pair suppression,
    same as v1, with one addition: when a position's own state is still
    unresolved (incomplete_coverage) but score_decision() determined the
    disqualifying point is plausibly inside a closure window, it attaches
    assumed_occupied_until_utc (NEVER used to resolve the trade's own
    state -- see score_decision) -- occupancy here uses that conservative
    estimate instead of the bare nominal deadline, so a position that will
    likely resolve via delayed closure execution is not prematurely freed
    for a new decision on the same pair while genuinely still pending."""
    by_pair: dict[str, list[dict]] = defaultdict(list)
    for dec in decisions:
        if dec.get("event_type") == "decision":
            by_pair[dec["pair"]].append(dec)

    ledger: list[dict] = []
    for pair, pair_decisions in by_pair.items():
        pair_decisions = sorted(pair_decisions, key=lambda d: d["actual_recording_time_utc"])
        open_until: datetime | None = None
        for dec in pair_decisions:
            published_at = _parse(dec["actual_recording_time_utc"])
            if open_until is not None and published_at < open_until:
                ledger.append({**dec, "executable": False, "state": "suppressed_existing_position",
                               "suppressed_until_utc": open_until.isoformat()})
                continue

            scored = score_decision(dec, quotes, now)
            ledger.append({**dec, "executable": True, **scored})

            if scored.get("assumed_entry_time_utc"):
                if scored.get("exit_time_utc"):
                    exit_dt = _parse(scored["exit_time_utc"])
                else:
                    entry_dt = _parse(scored["assumed_entry_time_utc"])
                    nominal_dt = entry_dt + timedelta(hours=dec["max_holding_time_hours"])
                    if scored.get("assumed_occupied_until_utc"):
                        exit_dt = max(nominal_dt, _parse(scored["assumed_occupied_until_utc"]))
                    else:
                        exit_dt = nominal_dt
                open_until = exit_dt if exit_dt > published_at else None
            else:
                open_until = None

    ledger.sort(key=lambda r: r["actual_recording_time_utc"])
    return ledger
