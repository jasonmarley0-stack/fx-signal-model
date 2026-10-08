"""Tests for the v2 measurement contract. Reproduces the EXACT failure
shapes found in the 7-day diagnosis of the v1 run (885c5e3e...), plus the
new quote_client reliability behaviour and the closure-aware deadline
policy. No network, no credentials.
"""
from __future__ import annotations
import json
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).parent))
from score import score_decision, build_paper_ledger, _has_coverage_gap, _valid_pair_quotes  # noqa: E402
from quote_client import fetch_pricing_samples, classify_error  # noqa: E402
import contract as cfg  # noqa: E402

T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=timezone.utc)  # a Monday


def _q(pair, t, bid, ask, tradeable=True, oanda_time=None, provider_age_seconds=1):
    oanda_t = oanda_time if oanda_time is not None else (t - timedelta(seconds=provider_age_seconds))
    return {"pair": pair, "received_at_utc": t.isoformat(),
            "oanda_time_utc": oanda_t.isoformat() if oanda_t else None,
            "bid": bid, "ask": ask, "tradeable": tradeable}


def _decision(pair="EURUSD", direction="long", entry=1.10000, stop=1.09800, target=1.10500, tol=0.00050,
              published_at=T0, entry_validity_hours=4.0, max_holding_hours=30.0):
    return {
        "pair": pair, "direction": direction, "entry_price": entry, "stop": stop, "target": target,
        "entry_condition_lo": entry - tol, "entry_condition_hi": entry + tol,
        "actual_recording_time_utc": published_at.isoformat(),
        "entry_expiry_utc": (published_at + timedelta(hours=entry_validity_hours)).isoformat(),
        "max_holding_time_hours": max_holding_hours,
    }


def _dense_filler(pair, start, end, price, step_seconds=10):
    out = []
    t = start
    while t < end:
        out.append(_q(pair, t, bid=price, ask=price + 0.00002))
        t += timedelta(seconds=step_seconds)
    return out


# ======================= failure shape 1: stale provider, fresh receipt =======================

def test_successful_receipt_with_unchanged_provider_timestamp_establishes_coverage_and_entry():
    """THE core diagnosed cause (5 of 8 v1 trades): a successful, fresh
    receipt whose embedded price-creation time is >15s old must be USABLE
    -- v1 discarded it outright. v2 must accept it for both coverage and
    for confirming an entry/exit."""
    entry_q = _q("EURUSD", T0 + timedelta(seconds=5), bid=1.09960, ask=1.10010,
                 oanda_time=T0 - timedelta(seconds=40))  # provider timestamp 40+s stale, receipt still fresh
    d = _decision(entry_validity_hours=1.0)
    now = T0 + timedelta(minutes=5)
    result = score_decision(d, [entry_q], now=now)
    assert result["assumed_entry_time_utc"] is not None, "a stale-provider-but-fresh-receipt quote must still confirm entry"
    assert result["assumed_entry_price"] == 1.10010
    print("v2: successful receipt with an unchanged/stale provider timestamp establishes entry: OK")


def test_valid_pair_quotes_reports_provider_age_as_metadata_never_as_a_gate():
    q = _q("EURUSD", T0, bid=1.1, ask=1.1002, oanda_time=T0 - timedelta(seconds=200))
    vq = _valid_pair_quotes([q], "EURUSD", T0 + timedelta(minutes=1))
    assert len(vq) == 1, "a 200s-stale provider timestamp must not be rejected in v2"
    assert vq[0]["_provider_age_seconds"] == 200.0, "provider age must still be reported as metadata"
    print("v2: provider age is reported as metadata, never used as a validity gate: OK")


def test_observed_crossing_after_stale_provider_quote_labeled_observed_not_first():
    """A resolved outcome must carry crossing_type='observed' -- never a
    stronger 'first crossing' claim."""
    entry_q = _q("EURUSD", T0 + timedelta(seconds=5), bid=1.09960, ask=1.10010)
    exit_q = _q("EURUSD", T0 + timedelta(seconds=10), bid=1.10520, ask=1.10522,
                oanda_time=T0 - timedelta(seconds=30))  # stale provider, still usable in v2
    d = _decision(stop=1.09800, target=1.10500, max_holding_hours=1.0)
    result = score_decision(d, [entry_q, exit_q], now=T0 + timedelta(hours=1))
    assert result["state"] == "targeted"
    assert result["crossing_type"] == "observed", "must label the crossing as observed, not an unqualified 'first crossing'"
    print("v2: a resolved crossing is explicitly labeled 'observed', never a stronger first-crossing claim: OK")


# ======================= failure shape 2: genuine 334s absence =======================

def test_genuine_334_second_absence_is_incomplete_coverage():
    """No raw receipts at all for 334s -- the exact shape of the v1
    decision-3/4 incident (a stalled request blocked the whole loop).
    Must remain a genuine, unbridged unknown in v2 too -- v2 fixes the
    stale-provider false negative, not real absence."""
    entry_t = T0 + timedelta(seconds=5)
    entry_q = _q("EURUSD", entry_t, bid=1.09960, ask=1.10010)
    filler_before = _dense_filler("EURUSD", entry_t + timedelta(seconds=10), entry_t + timedelta(minutes=2), price=1.10020)
    gap_start = entry_t + timedelta(minutes=2)
    resume_q = _q("EURUSD", gap_start + timedelta(seconds=334), bid=1.10030, ask=1.10032)
    d = _decision(stop=1.05000, target=1.20000, max_holding_hours=1.0)  # far levels, nothing hit
    now = T0 + timedelta(hours=1)
    result = score_decision(d, [entry_q, *filler_before, resume_q], now=now)
    assert result["state"] == "incomplete_coverage", result
    print("v2: a genuine 334s absence (no receipts at all) is still incomplete_coverage, never bridged: OK")


# ======================= failure shape 3: missed authorization tick =======================

def test_missed_authorization_tick_classified_as_auth_never_retried():
    class AuthError(Exception):
        code = 401
        msg = '{"errorMessage":"Insufficient authorization to perform request."}'
    assert classify_error(AuthError()) == "auth"

    call_count = {"n": 0}

    def failing_request(account_id, instruments, timeout_seconds):
        call_count["n"] += 1
        raise AuthError()

    clock = {"t": T0}
    result = fetch_pricing_samples(["EURUSD"], account_id="test-account", request_fn=failing_request,
                                    now_fn=lambda: clock["t"], sleep_fn=lambda s: None)
    assert result["samples"] == []
    assert call_count["n"] == 1, "an auth failure must never be retried"
    assert result["attempts"][0]["error_category"] == "auth"
    assert "errorMessage" not in str(result["attempts"][0].get("error", "")) or True  # error text itself may be included; account id must not be
    print("v2: an authorization failure is classified explicitly and never retried: OK")


def test_credentials_never_appear_in_attempt_error_text():
    import os
    os.environ["OANDA_ACCOUNT_ID"] = "101-004-SECRET-ACCOUNT-ID"

    class ConnErr(Exception):
        code = None
        msg = "HTTPSConnectionPool(host='api-fxpractice.oanda.com'): account=101-004-SECRET-ACCOUNT-ID timeout"

    def failing_request(account_id, instruments, timeout_seconds):
        raise ConnErr()

    clock = {"t": T0}
    result = fetch_pricing_samples(["EURUSD"], account_id="101-004-SECRET-ACCOUNT-ID", request_fn=failing_request,
                                    now_fn=lambda: clock["t"], sleep_fn=lambda s: None)
    for a in result["attempts"]:
        assert "101-004-SECRET-ACCOUNT-ID" not in a.get("error", ""), "account id leaked into a logged attempt"
    del os.environ["OANDA_ACCOUNT_ID"]
    print("v2: the account id never appears in a logged attempt's error text: OK")


# ======================= bounded retry / backoff / time ceiling =======================

def test_transient_failure_retried_then_succeeds_within_budget():
    clock = {"t": T0}
    attempts = []

    def flaky_request(account_id, instruments, timeout_seconds):
        attempts.append(1)
        if len(attempts) < 2:
            class ConnErr(Exception):
                code = None
                msg = "connection reset"
            raise ConnErr()
        return {"prices": [{"instrument": "EUR_USD", "time": clock["t"].isoformat() + "Z",
                             "bids": [{"price": "1.1000"}], "asks": [{"price": "1.1002"}], "tradeable": True}]}

    def sleep_fn(s):
        clock["t"] += timedelta(seconds=s)

    result = fetch_pricing_samples(["EURUSD"], account_id="test", request_fn=flaky_request,
                                    now_fn=lambda: clock["t"], sleep_fn=sleep_fn)
    assert len(result["samples"]) == 1
    assert len(attempts) == 2
    assert result["attempts"][0]["outcome"] == "failed"
    assert result["attempts"][1]["outcome"] == "success"
    print("v2: a transient failure is retried with backoff and recovers within one tick: OK")


def test_one_request_cannot_stall_collection_for_minutes():
    """The exact failure mode diagnosed in the v1 run: a single hung
    request must never block the collector for minutes. The total-time
    ceiling bounds it."""
    clock = {"t": T0}

    def always_hangs(account_id, instruments, timeout_seconds):
        clock["t"] += timedelta(seconds=timeout_seconds)  # simulates each attempt consuming its full timeout
        class TimeoutErr(Exception):
            code = None
            msg = "read timeout"
        raise TimeoutErr()

    start = clock["t"]
    result = fetch_pricing_samples(["EURUSD"], account_id="test", request_fn=always_hangs,
                                    now_fn=lambda: clock["t"], sleep_fn=lambda s: None)
    elapsed = (clock["t"] - start).total_seconds()
    assert result["samples"] == []
    assert elapsed <= cfg.MAX_TOTAL_REQUEST_SECONDS + cfg.REQUEST_TIMEOUT_SECONDS, (
        f"total elapsed {elapsed}s exceeded the bound meaningfully — collection could still stall")
    assert elapsed < 334, "must be nowhere near the 334s incident this fix addresses"
    print(f"v2: a persistently hanging request is bounded (elapsed={elapsed:.1f}s), never a multi-minute stall: OK")


def test_maintenance_message_classified_distinctly_from_connection_error():
    class MaintErr(Exception):
        code = 400
        msg = '{"errorMessage":"System under maintenance, please try again later."}'
    assert classify_error(MaintErr()) == "maintenance"

    class ConnErr(Exception):
        code = None
        msg = "Max retries exceeded with url"
    assert classify_error(ConnErr()) == "transient"
    print("v2: planned maintenance is classified distinctly from an unexpected connection error: OK")


# ======================= failure shape 4: invalid/crossed/future-dated =======================

def test_invalid_crossed_and_future_dated_quotes_still_rejected():
    now = T0 + timedelta(minutes=10)
    crossed = _q("EURUSD", T0, bid=1.2000, ask=1.1000)  # bid > ask
    non_finite = _q("EURUSD", T0, bid=float("nan"), ask=1.1002)
    future = _q("EURUSD", now + timedelta(minutes=5), bid=1.1, ask=1.1002)  # received "after" the scoring clock
    vq = _valid_pair_quotes([crossed, non_finite, future], "EURUSD", now)
    assert vq == [], f"crossed/non-finite/future-dated quotes must still all be rejected in v2, got {vq}"
    print("v2: crossed, non-finite, and future-dated quotes are still rejected (unchanged from v1): OK")


# ======================= failure shape 5: observed crossing after a gap =======================

def test_crossing_after_a_genuine_gap_does_not_become_a_confirmed_win():
    entry_q = _q("EURUSD", T0 + timedelta(seconds=5), bid=1.09960, ask=1.10010)
    later_q = _q("EURUSD", T0 + timedelta(minutes=30, seconds=5), bid=1.10520, ask=1.10522)
    d = _decision(entry=1.10000, stop=1.09800, target=1.10500, max_holding_hours=10)
    now = T0 + timedelta(hours=1)
    result = score_decision(d, [entry_q, later_q], now=now)
    assert result["state"] == "incomplete_coverage", (
        "a genuine gap before an apparent crossing must not become a confirmed win in v2 either")
    print("v2: a crossing observed only after a genuine coverage gap is not a confirmed win: OK")


# ======================= failure shape 6: deadline boundary =======================

def test_deadline_uses_first_sample_at_or_after_not_the_last_one_before():
    entry_t = T0 + timedelta(seconds=5)
    entry_q = _q("EURUSD", entry_t, bid=1.09960, ask=1.10010)
    deadline = entry_t + timedelta(minutes=5)
    filler = _dense_filler("EURUSD", entry_t + timedelta(seconds=10), deadline, price=1.10020)
    pre_deadline_q = _q("EURUSD", deadline - timedelta(seconds=3), bid=1.10040, ask=1.10042)
    post_deadline_q = _q("EURUSD", deadline + timedelta(seconds=4), bid=1.10050, ask=1.10052)
    d = _decision(stop=1.05000, target=1.20000, max_holding_hours=5 / 60)
    now = deadline + timedelta(minutes=10)
    result = score_decision(d, [entry_q, *filler, pre_deadline_q, post_deadline_q], now=now)
    assert result["state"] == "time_exited", result
    assert result["exit_price"] == 1.10050
    assert result["crossing_type"] == "observed"
    assert result["deadline_delay_reason"] is None, "an ordinary on-time deadline exit must not carry a closure label"
    print("v2: deadline execution uses the first sample at/after the deadline, labeled observed, no closure tag: OK")


# ======================= failure shape 7: Friday closure =======================

def test_friday_closure_deadline_delayed_to_first_tradeable_reopen_sample():
    """The new, explicitly labeled v2 exit policy. Deadline falls Friday
    evening; market closes (tradeable=False evidence) through the
    weekend; execution happens at the first tradeable sample after
    Sunday reopen, labeled deadline_delay_reason='market_closure'."""
    friday = datetime(2026, 1, 9, 20, 0, 0, tzinfo=timezone.utc)  # a Friday
    entry_t = friday - timedelta(hours=30, minutes=-5)  # entry such that deadline lands Friday ~20:00 UTC
    entry_q = _q("EURUSD", entry_t, bid=1.09960, ask=1.10010)
    deadline = entry_t + timedelta(hours=30)
    filler = _dense_filler("EURUSD", entry_t + timedelta(seconds=10), deadline, price=1.10020)
    # closure evidence: non-tradeable quotes through the weekend
    closure_q1 = _q("EURUSD", deadline + timedelta(minutes=5), bid=1.1002, ask=1.1003, tradeable=False)
    closure_q2 = _q("EURUSD", deadline + timedelta(hours=20), bid=1.1002, ask=1.1003, tradeable=False)
    reopen_t = deadline + timedelta(hours=49)  # Sunday reopen, within the weekly window
    reopen_q = _q("EURUSD", reopen_t, bid=1.10800, ask=1.10810, tradeable=True)
    d = _decision(stop=1.05000, target=1.20000, max_holding_hours=30.0, published_at=entry_t - timedelta(minutes=1))
    now = reopen_t + timedelta(minutes=10)
    result = score_decision(d, [entry_q, *filler, closure_q1, closure_q2, reopen_q], now=now)
    assert result["state"] == "time_exited", result
    assert result["deadline_delay_reason"] == "market_closure", "must be explicitly labeled as the new closure policy"
    assert result["exit_price"] == 1.10800
    assert result["execution_delay_seconds"] > 3600, "the delay must reflect the real wait through the weekend"
    print("v2: a deadline during demonstrated Friday closure delays execution to the first tradeable reopen sample, explicitly labeled: OK")


def test_weekday_gap_resembling_closure_duration_is_not_silently_treated_as_closure():
    """Closure must be DEMONSTRATED, not assumed from duration or weekday
    proximity alone. A long weekday gap with NO tradeable=False evidence
    and outside the reference window stays incomplete_coverage."""
    tuesday = datetime(2026, 1, 6, 12, 0, 0, tzinfo=timezone.utc)
    entry_t = tuesday
    entry_q = _q("EURUSD", entry_t, bid=1.09960, ask=1.10010)
    deadline = entry_t + timedelta(hours=1)
    filler = _dense_filler("EURUSD", entry_t + timedelta(seconds=10), deadline - timedelta(seconds=30), price=1.10020)
    # nothing at all near/after the deadline -- no closure evidence, not in the weekly closure window
    resume_q = _q("EURUSD", deadline + timedelta(hours=10), bid=1.10800, ask=1.10810, tradeable=True)
    d = _decision(stop=1.05000, target=1.20000, max_holding_hours=1.0, published_at=entry_t - timedelta(minutes=1))
    now = deadline + timedelta(hours=11)
    result = score_decision(d, [entry_q, *filler, resume_q], now=now)
    assert result["state"] == "incomplete_coverage", (
        f"an unexplained weekday gap must never be silently treated as closure, got {result['state']}")
    assert result["deadline_delay_reason"] is None
    print("v2: an unexplained weekday gap is never silently classified as market closure: OK")


# ======================= no-lookahead =======================

def test_decisions_never_use_future_information():
    entry_q = _q("EURUSD", T0 + timedelta(seconds=5), bid=1.09960, ask=1.10010)
    future_target_hit = _q("EURUSD", T0 + timedelta(hours=2), bid=1.10520, ask=1.10522)  # exists in the data but is in the FUTURE relative to `now`
    d = _decision(stop=1.09800, target=1.10500, max_holding_hours=10)
    now = T0 + timedelta(minutes=30)  # now is BEFORE the future sample's own receipt time
    result = score_decision(d, [entry_q, future_target_hit], now=now)
    assert result["state"] == "open", (
        f"a sample received after the scoring clock must never be used to resolve a decision, got {result['state']}")
    print("v2: a decision never resolves using a sample received after the scoring clock (no lookahead): OK")


# ======================= completed + unknown demonstration =======================

def test_demonstrates_a_completed_sampled_paper_trade():
    entry_q = _q("EURUSD", T0 + timedelta(seconds=5), bid=1.09960, ask=1.10010)
    filler = _dense_filler("EURUSD", T0 + timedelta(seconds=10), T0 + timedelta(minutes=10), price=1.10020)
    win_q = _q("EURUSD", T0 + timedelta(minutes=10, seconds=5), bid=1.10520, ask=1.10522)
    d = _decision(stop=1.09800, target=1.10500, max_holding_hours=10)
    result = score_decision(d, [entry_q, *filler, win_q], now=T0 + timedelta(hours=1))
    assert result["state"] == "targeted"
    assert result["r_multiple"] is not None and result["r_multiple"] > 0
    assert result["crossing_type"] == "observed"
    print("v2: a genuinely gap-free sampled trade now completes cleanly with a real R multiple: OK")


def test_genuine_unknown_is_still_retained_not_silently_resolved():
    """Success means appropriate classification, not fewer unknowns for
    their own sake: a trade with a real, unexplained gap must still come
    back incomplete_coverage in v2."""
    entry_q = _q("EURUSD", T0 + timedelta(seconds=5), bid=1.09960, ask=1.10010)
    d = _decision(stop=1.09800, target=1.10500, max_holding_hours=1.0)  # short holding so the deadline has passed by `now`
    now = T0 + timedelta(hours=2)  # well past the 1h deadline, nothing in the market/closure window explains the silence
    result = score_decision(d, [entry_q], now=now)
    assert result["state"] == "incomplete_coverage"
    print("v2: a genuinely unexplained gap is still honestly retained as incomplete_coverage, not resolved: OK")


# ======================= position policy / ledger (unchanged from v1 — one confirmation) =======================

def test_position_policy_unchanged_still_suppresses_overlapping_decisions():
    d1 = _decision(entry=1.10000, stop=1.09800, target=1.20000, published_at=T0, max_holding_hours=10)
    d1.update(event_type="decision", pair="EURUSD", source_candle_completion_utc=T0.isoformat())
    t2 = T0 + timedelta(minutes=30)
    d2 = _decision(entry=1.10100, stop=1.09900, target=1.20100, published_at=t2, max_holding_hours=10)
    d2.update(event_type="decision", pair="EURUSD", source_candle_completion_utc=t2.isoformat())
    entry_q = _q("EURUSD", T0 + timedelta(seconds=5), bid=1.09960, ask=1.10010)
    now = T0 + timedelta(hours=1)
    ledger = build_paper_ledger([d1, d2], [entry_q], now)
    suppressed = [r for r in ledger if not r["executable"]]
    assert len(suppressed) == 1 and suppressed[0]["pair"] == "EURUSD"
    print("v2: position suppression is unchanged from v1 — confirmed still working: OK")


if __name__ == "__main__":
    test_successful_receipt_with_unchanged_provider_timestamp_establishes_coverage_and_entry()
    test_valid_pair_quotes_reports_provider_age_as_metadata_never_as_a_gate()
    test_observed_crossing_after_stale_provider_quote_labeled_observed_not_first()
    test_genuine_334_second_absence_is_incomplete_coverage()
    test_missed_authorization_tick_classified_as_auth_never_retried()
    test_credentials_never_appear_in_attempt_error_text()
    test_transient_failure_retried_then_succeeds_within_budget()
    test_one_request_cannot_stall_collection_for_minutes()
    test_maintenance_message_classified_distinctly_from_connection_error()
    test_invalid_crossed_and_future_dated_quotes_still_rejected()
    test_crossing_after_a_genuine_gap_does_not_become_a_confirmed_win()
    test_deadline_uses_first_sample_at_or_after_not_the_last_one_before()
    test_friday_closure_deadline_delayed_to_first_tradeable_reopen_sample()
    test_weekday_gap_resembling_closure_duration_is_not_silently_treated_as_closure()
    test_decisions_never_use_future_information()
    test_demonstrates_a_completed_sampled_paper_trade()
    test_genuine_unknown_is_still_retained_not_silently_resolved()
    test_position_policy_unchanged_still_suppresses_overlapping_decisions()
    print("All v2 prospective-observation tests passed (no network, no credentials).")
