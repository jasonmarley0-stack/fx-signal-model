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

Corrected (see CODEX_PROSPECTIVE_CORRECTIONS.md for the full defect list
this addresses): quote validity (provider age, tradeability, future-clock
samples, non-finite/non-positive prices, and CROSSED quotes where
bid > ask -- none of these may establish a fill or coverage) is checked
before a quote may establish anything; coverage gaps are checked through
the FULL relevant interval (including the trailing gap to a window's end,
and gaps that could hide an earlier exit); the holding deadline is
resolved with ONE consistent rule (first valid sample AT OR AFTER the
deadline, within the permitted delay, never the last sample before it,
never reclassified by movement after that sample).
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
    "Prospective PAPER result — a hypothetical decision scored against real, but discretely SAMPLED, "
    "bid/ask quotes. No order was placed; nothing here implies a real fill.",
    "Price movement BETWEEN samples is unobserved and unobservable here — a real, stated limitation "
    "distinct from the offline replay's continuous M30-candle coverage.",
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
    """A price is usable only if it's a real, finite, positive number --
    None/NaN/inf/zero/negative are all explicit uncertainty, never
    silently coerced into something usable."""
    try:
        f = float(p)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f


def _valid_pair_quotes(quotes: list[dict], pair: str, now: datetime) -> list[dict]:
    """Filters to quotes that may actually be used to establish an entry,
    exit, or coverage: real bid+ask (finite, positive, and NOT crossed --
    bid <= ask; a crossed quote, e.g. from a bad tick or data-feed glitch,
    is a malformed observation, never a usable one, and must never be
    allowed to establish an instant fill), tradeable, a parseable provider
    timestamp (oanda_time_utc) no older than QUOTE_MAX_PROVIDER_AGE_SECONDS
    relative to receipt, and received_at_utc no later than `now` (the
    scoring clock) -- a sample "received" after the moment being scored
    must never be used, whether that's a clock anomaly or this function
    being asked about an earlier point in time than when the sample
    actually arrived. Each returned dict gains a parsed '_recv' datetime
    and validated float '_bid'/'_ask' for convenience; invalid/unusable
    quotes are dropped here, never repaired -- the raw observation is
    still retained in quotes_log.jsonl regardless, this filter only
    controls what may be used to SCORE a decision."""
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
            continue  # missing/unparseable provider timestamp -> explicit uncertainty, not usable
        provider_age = (recv - oanda_t).total_seconds()
        if provider_age < 0 or provider_age > cfg.QUOTE_MAX_PROVIDER_AGE_SECONDS:
            continue
        out.append({**q, "_recv": recv, "bid": bid, "ask": ask})
    out.sort(key=lambda q: q["_recv"])
    return out


def _has_coverage_gap(valid_quotes: list[dict], start: datetime, end: datetime) -> bool:
    """True if there is any stretch within [start, end] — at the start, in
    the middle, OR at the end — longer than QUOTE_STALENESS_SECONDS with
    no valid sample. A single early sample followed by silence for the
    rest of the window must be flagged: checking only the leading and
    inter-sample gaps (the earlier, defective version of this function)
    let one stale sample near the window's start disguise a fully
    uncovered remainder as "no gap"."""
    window = [q for q in valid_quotes if start <= q["_recv"] <= end]
    if not window:
        return True
    if (window[0]["_recv"] - start).total_seconds() > cfg.QUOTE_STALENESS_SECONDS:
        return True
    for a, b in zip(window, window[1:]):
        if (b["_recv"] - a["_recv"]).total_seconds() > cfg.QUOTE_STALENESS_SECONDS:
            return True
    if (end - window[-1]["_recv"]).total_seconds() > cfg.QUOTE_STALENESS_SECONDS:
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
    vq = _valid_pair_quotes(quotes, pair, now)

    result = {
        "state": None, "assumed_entry_time_utc": None, "assumed_entry_price": None, "same_sample_exit": False,
        "exit_time_utc": None, "exit_price": None, "r_multiple": None,
        "scheduled_exit_time_utc": None, "execution_delay_seconds": None,
        "result_type": "prospective_paper", "caveats": list(BASE_CAVEATS),
    }

    # --- Entry scan: first valid sample, entry-side price in range ---
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

    # --- Same-sample exit: the entry-confirming sample's own exit-side
    # price may already show a breach -- a real, near-instantaneous
    # fill+exit, priced at that actually-observed quote. ---
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
    hit_scan_end = min(max_exit_time, now)

    # --- Gap-aware exit scan: a coverage gap BEFORE a candidate sample
    # means we cannot trust that sample is really the first thing that
    # happened -- a later "clean" hit after an unobserved stretch must not
    # be reported as a confirmed win/loss. ---
    last_checked = entry_time
    for q in vq:
        t = q["_recv"]
        if t <= entry_time:
            continue
        if t > hit_scan_end:
            break
        if (t - last_checked).total_seconds() > cfg.QUOTE_STALENESS_SECONDS:
            result["state"] = "incomplete_coverage"
            return result
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
        last_checked = t

    if max_exit_time <= now:
        # Require preceding coverage up to the deadline itself -- a gap
        # here could be hiding an earlier stop/target hit we'd otherwise
        # have seen (the defect: "the entered-position exit scan also
        # ignores coverage gaps").
        if _has_coverage_gap(vq, last_checked, max_exit_time):
            result["state"] = "incomplete_coverage"
            return result
        # Deadline execution: one consistent rule. A scheduled time exit
        # can only be acted on once the deadline has genuinely arrived --
        # priced at the FIRST valid sample AT OR AFTER max_exit_time,
        # within the permitted staleness/delay window. Never the last
        # sample BEFORE the deadline (contract.py always described "the
        # next sample after the deadline"; using the prior sample was a
        # real defect, fixed here). Never reclassified by any stop/target
        # movement observed after that single pricing sample.
        post_deadline = [q for q in vq if q["_recv"] >= max_exit_time]
        if not post_deadline or (post_deadline[0]["_recv"] - max_exit_time).total_seconds() > cfg.QUOTE_STALENESS_SECONDS:
            result["state"] = "incomplete_coverage"
            return result
        exit_sample = post_deadline[0]
        exit_time = exit_sample["_recv"]
        exit_price = exit_sample[exit_side]
        result.update(
            state="time_exited", exit_time_utc=exit_time.isoformat(), exit_price=exit_price,
            scheduled_exit_time_utc=max_exit_time.isoformat(),
            execution_delay_seconds=(exit_time - max_exit_time).total_seconds(),
            r_multiple=(d * (exit_price - entry_price) / risk if risk else None),
        )
    else:
        result["state"] = "open"
    return result


def score_all(decisions: list[dict], quotes: list[dict], now: datetime) -> list[dict]:
    """Scores every raw decision independently, with NO position
    suppression -- kept for inspecting what the strategy would have said
    at every candle regardless of overlap. report.py's KPIs use
    build_paper_ledger() below instead, which is what "executable
    opportunities" actually means under this system's position policy."""
    return [{**dec, **score_decision(dec, quotes, now)} for dec in decisions if dec.get("event_type") == "decision"]


def build_paper_ledger(decisions: list[dict], quotes: list[dict], now: datetime) -> list[dict]:
    """Applies chronological one-entered-position-per-pair suppression, a
    defect fix: score_all() scored every decision independently, so two
    decisions overlapping the same pair could both be counted as separate
    executable opportunities even while one already held an entered
    position. Every raw decision is retained here (`executable: False` for
    a suppressed one, never dropped), but only `executable: True` rows
    count as opportunities in report.py's KPIs.

    An entered position whose outcome is unresolved (ambiguous_intrabar_
    exit, incomplete_coverage, or still open) conservatively reserves the
    pair through entry_time + max_holding_time_hours, the same discipline
    research/offline_comparison/replay_engine.py uses -- an unknown
    outcome must not silently free the pair for a new entry."""
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
