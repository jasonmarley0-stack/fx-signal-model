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
    """v2: validity is receipt-time-only. A quote is usable when it has a
    real, finite, positive, non-crossed bid/ask; tradeable=True; and a
    receipt time not in the future relative to the scoring clock. Price-
    creation age (OANDA's own "time" field) is NEVER a gate here — see
    MEASUREMENT_CONTRACT.md section 2. It is still computed and attached
    as `_provider_age_seconds` (None if unparseable) purely as reported
    metadata, never used to accept or reject."""
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
        provider_age = (recv - oanda_t).total_seconds() if oanda_t is not None else None
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


def _detect_closure(raw_pair_quotes: list[dict], deadline: datetime, now: datetime) -> dict | None:
    """Demonstrated, not assumed (MEASUREMENT_CONTRACT.md section 4): only
    returns a closure finding when BOTH (a) the deadline falls inside the
    weekly closure reference window, AND (b) the actual recorded evidence
    in [deadline, candidate reopen] is consistent with a closed market —
    either total absence of any raw receipt, or every raw receipt present
    is tradeable=False. Returns None (no closure finding — stay
    incomplete_coverage) the instant that evidence is contradicted, e.g.
    a tradeable=True quote appears where closure would predict none.

    Returns {"reopen_quote": <first valid quote after the closed stretch>,
    or None if the closed stretch hasn't ended within the sanity ceiling}.
    """
    if not _in_closure_reference_window(deadline):
        return None
    ceiling = deadline + timedelta(hours=cfg.MAX_CLOSURE_DEADLINE_DELAY_HOURS)
    scan_end = min(ceiling, now)
    window = [q for q in raw_pair_quotes if deadline <= _try_parse(q.get("received_at_utc")) <= scan_end]
    reopen_quote = None
    for q in sorted(window, key=lambda q: q["received_at_utc"]):
        if q.get("tradeable", False):
            bid, ask = _valid_price(q.get("bid")), _valid_price(q.get("ask"))
            if bid is not None and ask is not None and bid <= ask:
                recv = _try_parse(q.get("received_at_utc"))
                reopen_quote = {**q, "_recv": recv}
                break
        # a non-tradeable (or invalid) quote during the candidate window is
        # exactly what closure predicts -- keep scanning for reopen.
    if reopen_quote is None:
        return None  # either no evidence at all yet, or still inside the ceiling with no reopen found -- stay unknown
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
    last_checked = entry_time
    for q in vq:
        t = q["_recv"]
        if t <= entry_time:
            continue
        if t > hit_scan_end:
            break
        if (t - last_checked).total_seconds() > cfg.COVERAGE_GAP_SECONDS:
            result["state"] = "incomplete_coverage"
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
            result["state"] = "incomplete_coverage"
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
            result.update(
                state="time_exited", crossing_type="observed", exit_time_utc=exit_time.isoformat(), exit_price=exit_price,
                scheduled_exit_time_utc=max_exit_time.isoformat(),
                execution_delay_seconds=(exit_time - max_exit_time).total_seconds(),
                deadline_delay_reason="market_closure",
                r_multiple=(d * (exit_price - entry_price) / risk if risk else None),
            )
            result["caveats"] = result["caveats"] + [
                "Deadline execution delayed past the nominal 30-hour boundary: the deadline fell within a "
                "demonstrated market-closure window (empirical tradeable=false / absence evidence), and no fill "
                "was possible against a closed market in a real account either. Executed at the first tradeable "
                "sample after reopening. This is a new, explicitly labeled v2 exit policy — see "
                "MEASUREMENT_CONTRACT.md section 4 — never silently substituted into a v1-contract comparison."]
            return result
        result["state"] = "incomplete_coverage"
        return result
    else:
        result["state"] = "open"
    return result


def score_all(decisions: list[dict], quotes: list[dict], now: datetime) -> list[dict]:
    return [{**dec, **score_decision(dec, quotes, now)} for dec in decisions if dec.get("event_type") == "decision"]


def build_paper_ledger(decisions: list[dict], quotes: list[dict], now: datetime) -> list[dict]:
    """Unchanged suppression logic from v1 — pair-local chronological
    one-entered-position-per-pair suppression. Not reopened by v2; the
    measurement contract change lives entirely in score_decision() above."""
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
                    exit_dt = entry_dt + timedelta(hours=dec["max_holding_time_hours"])
                open_until = exit_dt if exit_dt > published_at else None
            else:
                open_until = None

    ledger.sort(key=lambda r: r["actual_recording_time_utc"])
    return ledger
