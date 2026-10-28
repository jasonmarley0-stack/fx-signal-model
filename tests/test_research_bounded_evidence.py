from datetime import datetime, timedelta, timezone
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import research_snapshot_publisher as pub
from research_quote_index import QuoteIndex, QuoteWindowTooLarge
from research_gap_evidence import reconstruct
from research_evidence_audit import fetch_gap_candles

T = datetime(2026, 10, 22, 20, 59, tzinfo=timezone.utc)  # Thursday rollover


def q(seconds, price=1.1, tradeable=True):
    t = T + timedelta(seconds=seconds)
    return dict(pair="EURUSD", received_at_utc=t.isoformat(), oanda_time_utc=t.isoformat(),
                bid=price, ask=price + .0001, tradeable=tradeable)


def row(holding=30):
    return dict(event_type="decision", pair="EURUSD", direction="long", stop=1.09, target=1.12,
                actual_recording_time_utc=T.isoformat(), entry_expiry_utc=(T + timedelta(hours=4)).isoformat(),
                entry_condition_lo=1.1, entry_condition_hi=1.101, max_holding_time_hours=holding,
                assumed_entry_time_utc=T.isoformat(), assumed_entry_price=1.1001, state="incomplete_coverage",
                source_candle_completion_utc=T.isoformat(), decision_delay_seconds=0)


def bars(start=0, end=30, **changes):
    result = []
    for s in range(start, end, 5):
        b = dict(pair="EURUSD", side="bid", complete=True, start_utc=(T + timedelta(seconds=s)).isoformat(),
                 end_utc=(T + timedelta(seconds=s + 5)).isoformat(), open=1.1, high=1.101, low=1.099,
                 close=1.1, fetched_at_utc=(T + timedelta(seconds=50)).isoformat(), evidence_sha256="f" * 64)
        b.update(changes)
        result.append(b)
    return result


def test_index_incremental_partial_line_rotation_and_truncation(tmp_path):
    source = tmp_path / "quotes.jsonl"
    first, second = json.dumps(q(0)) + "\n", json.dumps(q(5)) + "\n"
    source.write_text(first + second[:20])
    idx = QuoteIndex(tmp_path / "index.sqlite")
    try:
        assert idx.sync(source) == 1
        assert idx.sync(source) == 1
        with source.open("a") as f:
            f.write(second[20:])
        assert idx.sync(source) == 2
        assert len(idx.window("EURUSD", T, T + timedelta(minutes=1))) == 2
        # A rewritten checkpoint and then an inode replacement both rebuild.
        source.write_text(json.dumps(q(10)) + "\n")
        assert idx.sync(source) == 1
        replacement = tmp_path / "replacement"
        replacement.write_text(first)
        replacement.replace(source)
        assert idx.sync(source) == 1
        assert idx.window("EURUSD", T, T)[0] == q(0)
    finally:
        idx.close()


def test_window_guard_fails_before_allocating_dense_window(tmp_path, monkeypatch):
    source = tmp_path / "quotes.jsonl"
    source.write_text("".join(json.dumps(q(s)) + "\n" for s in range(10)))
    idx = QuoteIndex(tmp_path / "index.sqlite")
    try:
        idx.sync(source)
        monkeypatch.setattr(idx, "MAX_WINDOW_ROWS", 5)
        with pytest.raises(QuoteWindowTooLarge):
            idx.window("EURUSD", T, T + timedelta(seconds=10))
    finally:
        idx.close()


def test_per_decision_parity_occupancy_and_no_pair_history_allocation(tmp_path, monkeypatch):
    fixture = Path(__file__).parent / "fixtures/pinned_observer_v2_dev"
    checkout = tmp_path / "observer"
    shutil.copytree(fixture, checkout)
    _, frozen, _ = pub._import_observer_modules(checkout, "research/prospective_baseline_v2")
    decisions = [row(holding=.01)]
    decisions.append({**row(holding=.01), "actual_recording_time_utc": (T + timedelta(seconds=10)).isoformat()})
    later = T + timedelta(days=7)
    decisions.append({**row(holding=.01), "actual_recording_time_utc": later.isoformat(),
                      "entry_expiry_utc": (later + timedelta(hours=4)).isoformat()})
    quotes = [q(s) for s in range(0, 46, 5)]
    quotes += [{**x, "received_at_utc": (later + timedelta(seconds=s)).isoformat(),
                "oanda_time_utc": (later + timedelta(seconds=s)).isoformat()} for s, x in zip(range(0, 46, 5), quotes)]
    source = tmp_path / "quotes.jsonl"
    source.write_text("".join(json.dumps(x) + "\n" for x in quotes))
    expected = frozen.build_paper_ledger(decisions, quotes, later + timedelta(minutes=1))
    original = frozen.score_decision
    sizes = []
    window = QuoteIndex.window

    def watched(self, *args):
        found = window(self, *args)
        sizes.append(len(found))
        return found

    monkeypatch.setattr(QuoteIndex, "window", watched)
    actual, scanned, _ = pub._bounded_ledger(frozen, decisions, source, later + timedelta(minutes=1), 15, 216000)
    assert actual == expected
    assert actual[1]["state"] == "suppressed_existing_position"
    assert scanned == 20 and sizes == [10, 10]
    assert frozen.score_decision is original  # no mutation of frozen module


def test_health_history_aggregates_a_single_use_stream():
    class Once:
        used = False
        def __iter__(self):
            assert not self.used
            self.used = True
            yield {"event_type": "no_signal"}
            yield {"event_type": "quote_attempt", "outcome": "failed"}
    kpi = pub._kpi_from_frozen_ledger([], [], Once(), T)
    assert kpi["counts"]["no_signal_ticks"] == 1
    assert kpi["operational"]["recording_failures"] == 1


def test_evidence_resolves_small_outage_only_when_archive_proves_no_touch():
    d = row(holding=40 / 3600)
    quotes = [q(0), q(5), q(30), q(35), q(40)]
    assert reconstruct(d, quotes, [], T + timedelta(seconds=60))["state"] == "unknown"
    result = reconstruct(d, quotes, bars(), T + timedelta(seconds=60))
    assert result["state"] == "time_exited"
    assert result["proofs"][0]["kind"] == "historical_exit_side_envelope_excludes_levels"
    assert d["state"] == "incomplete_coverage"  # original unchanged


@pytest.mark.parametrize("change", [{"low": 1.08}, {"high": 1.13}, {"side": "ask"},
    {"complete": False}, {"evidence_sha256": ""}, {"high": float("nan")},
    {"fetched_at_utc": (T + timedelta(days=1)).isoformat()}, {"start_utc": "bad"}])
def test_bad_or_crossing_candle_never_recovers_a_gap(change):
    d = row(holding=40 / 3600)
    result = reconstruct(d, [q(0), q(30), q(40)], bars(**change), T + timedelta(seconds=60))
    assert result["state"] == "unknown" and result["r_multiple"] is None


def test_one_missing_archive_bar_remains_unknown():
    archive = bars()
    del archive[2]
    assert reconstruct(row(), [q(0), q(30)], archive, T + timedelta(seconds=60))["state"] == "unknown"


def test_proven_daily_nontradeability_bridges_without_weekend_guessing():
    quotes = [q(0)] + [q(s, tradeable=False) for s in range(5, 360, 5)] + [q(360)]
    result = reconstruct(row(holding=300 / 3600), quotes, [], T + timedelta(seconds=400))
    assert result["state"] == "time_exited"
    assert result["deadline_delay_reason"] == "evidenced_nontradeability"
    assert result["exit_time_utc"] == q(360)["received_at_utc"]


def test_reopen_gap_stop_uses_actual_quote_not_stop_level():
    quotes = [q(0)] + [q(s, tradeable=False) for s in range(5, 360, 5)] + [q(360, price=1.08)]
    result = reconstruct(row(holding=300 / 3600), quotes, [], T + timedelta(seconds=400))
    assert result["state"] == "stopped" and result["exit_price"] == 1.08
    assert result["r_multiple"] < -1


@pytest.mark.parametrize("corrupt", ["missing", "future", "absent_flag"])
def test_silence_or_corrupt_closure_evidence_never_bridged(corrupt):
    quotes = [q(0)] + [q(s, tradeable=False) for s in range(5, 360, 5)] + [q(360)]
    if corrupt == "missing":
        quotes = [quotes[0], quotes[-1]]
    elif corrupt == "future":
        for sample in quotes[1:-1]:
            sample["oanda_time_utc"] = (T + timedelta(days=1)).isoformat()
    else:
        for sample in quotes[1:-1]:
            del sample["tradeable"]
    assert reconstruct(row(holding=300 / 3600), quotes, [], T + timedelta(seconds=400))["state"] == "unknown"


def test_postdeadline_move_never_becomes_a_target():
    quotes = [q(0), q(5), q(10), q(15, price=1.13)]
    result = reconstruct(row(holding=12 / 3600), quotes, [], T + timedelta(seconds=60))
    assert result["state"] == "time_exited"  # not a post-deadline target


def test_missing_deadline_quote_not_replaced_by_old_close():
    result = reconstruct(row(holding=40 / 3600), [q(s) for s in range(0, 40, 5)], bars(), T + timedelta(seconds=60))
    assert result["state"] == "unknown" and result["exit_price"] is None


def test_archive_fetch_practice_guard_precedes_any_request(tmp_path, monkeypatch):
    monkeypatch.setenv("OANDA_ENVIRONMENT", "live")
    def forbidden(*args):
        raise AssertionError("Network call must never occur")
    with pytest.raises(RuntimeError, match="practice"):
        fetch_gap_candles("EURUSD", T, T + timedelta(seconds=30), tmp_path, forbidden)


def test_archive_fetch_retains_bid_ask_sides_and_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("OANDA_ENVIRONMENT", "practice")
    def response(instrument, params):
        assert instrument == "EUR_USD" and params["price"] == "BA" and params["granularity"] == "S5"
        return {"candles": [{"time": T.isoformat(), "complete": True,
                              "bid": {"o": "1.1", "h": "1.101", "l": "1.099", "c": "1.1"},
                              "ask": {"o": "1.1001", "h": "1.1011", "l": "1.0991", "c": "1.1001"}}]}
    result = fetch_gap_candles("EURUSD", T, T + timedelta(seconds=5), tmp_path, response,
                               lambda: T + timedelta(seconds=60))
    assert {r["side"] for r in result} == {"bid", "ask"}
    assert result[0]["evidence_sha256"] == result[1]["evidence_sha256"]
    assert (tmp_path / (result[0]["evidence_sha256"] + ".json")).exists()


def test_future_quote_cannot_establish_reopening():
    quotes = [q(0)] + [q(s, tradeable=False) for s in range(5, 360, 5)] + [q(360)]
    quotes[-1]["oanda_time_utc"] = (T + timedelta(days=1)).isoformat()
    result = reconstruct(row(holding=300 / 3600), quotes, [], T + timedelta(seconds=400))
    assert result["state"] != "time_exited" and result["r_multiple"] is None


def test_short_exit_uses_ask_not_bid():
    d = {**row(holding=10 / 3600), "direction": "short", "stop": 1.12, "target": 1.09,
         "assumed_entry_price": 1.1}
    result = reconstruct(d, [q(0), q(5), q(10)], [], T + timedelta(seconds=60))
    assert result["state"] == "time_exited" and result["r_multiple"] < 0
    assert result["exit_price"] == 1.1001


def test_conflicting_duplicate_candle_cannot_hide_a_touch():
    archive = bars()
    archive.append({**archive[1], "low": 1.08})
    result = reconstruct(row(), [q(0), q(30)], archive, T + timedelta(seconds=60))
    assert result["state"] == "unknown"


def test_offline_audit_end_to_end_preserves_observer_and_live_output(tmp_path):
    from research_evidence_audit import audit
    import hashlib
    checkout = tmp_path / "observer"
    shutil.copytree(Path(__file__).parent / "fixtures/pinned_observer_v2_dev", checkout)
    logs = checkout / "research/prospective_baseline_v2/logs"
    logs.mkdir()
    _, _, report = pub._import_observer_modules(checkout, "research/prospective_baseline_v2")
    report.load_or_create_manifest(logs)  # initialise synthetic fixture only
    (logs / "decisions_log.jsonl").write_text(json.dumps(row(holding=40 / 3600)) + "\n")
    (logs / "quotes_log.jsonl").write_text("".join(json.dumps(q(s)) + "\n" for s in range(0, 45, 5)))
    before = {p.relative_to(logs): hashlib.sha256(p.read_bytes()).hexdigest() for p in logs.iterdir()}
    out = tmp_path / "audit"
    result = audit(checkout, out)
    assert result["network_enabled"] is False
    assert result["ledger"][0]["state"] == "time_exited"
    assert result["live_kpi_unchanged"] is True
    assert not (out / "research_kpi_v2.json").exists()
    after = {p.relative_to(logs): hashlib.sha256(p.read_bytes()).hexdigest() for p in logs.iterdir()}
    assert before == after
