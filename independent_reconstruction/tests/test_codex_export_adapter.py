import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_export_adapter as adapter

INPUT_FIELDS = ["trade_id", "run_id", "pair", "direction", "actual_recording_time_utc",
                "entry_timestamp_utc", "entry_price", "stop_loss", "target", "original_deadline_utc",
                "adjusted_deadline_utc", "price_basis", "spread_assumptions",
                "existing_outcome_classification", "evidence_gap_details", "measurement_contract_version"]
EVIDENCE_FIELDS = ["trade_id", "run_id", "pair", "received_at_utc", "oanda_time_utc", "bid", "ask",
                   "tradeable", "window_start_utc", "nominal_deadline_utc"]


def write_csv(path, rows, fieldnames):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def input_row(**overrides):
    row = {"trade_id": "v2-001", "run_id": "run-A", "pair": "EURUSD", "direction": "long",
           "actual_recording_time_utc": "2026-10-21T01:00:00+00:00",
           "entry_timestamp_utc": "2026-10-21T01:01:13.107750+00:00", "entry_price": "1.10000",
           "stop_loss": "1.09800", "target": "1.10200",
           "original_deadline_utc": "2026-10-22T07:01:13.107750+00:00", "adjusted_deadline_utc": "",
           "price_basis": "long: ask entry/bid exit; short: bid entry/ask exit",
           "spread_assumptions": "sampled OANDA bid/ask", "existing_outcome_classification": "incomplete_coverage",
           "evidence_gap_details": "See V2_TRADE_FORENSIC_AUDIT.csv", "measurement_contract_version": "v2"}
    row.update(overrides)
    return row


def test_entered_trade_is_loaded_with_mapped_fields(tmp_path):
    p = tmp_path / "input.csv"
    write_csv(p, [input_row()], INPUT_FIELDS)
    trades = adapter.load_trades(p)
    assert len(trades) == 1
    t = trades[0]
    assert t["decision_id"] == "v2-001"
    assert t["pair"] == "EURUSD"
    assert t["stop"] == 1.098
    assert t["target"] == 1.102
    assert t["assumed_entry_price"] == 1.1
    assert t["max_holding_time_hours"] == 30.0
    assert t["original_state"] == "incomplete_coverage"
    assert t["_errors"] == []


def test_suppressed_row_with_blank_trade_id_is_excluded(tmp_path):
    p = tmp_path / "input.csv"
    write_csv(p, [input_row(trade_id="", entry_price="", entry_timestamp_utc="",
                            existing_outcome_classification="suppressed_existing_position")], INPUT_FIELDS)
    trades = adapter.load_trades(p)
    assert trades == []


def test_never_entered_row_with_blank_trade_id_is_excluded(tmp_path):
    p = tmp_path / "input.csv"
    write_csv(p, [input_row(trade_id="", entry_price="", entry_timestamp_utc="",
                            existing_outcome_classification="insufficient_data_entry")], INPUT_FIELDS)
    assert adapter.load_trades(p) == []


def test_deadline_inversion_is_exact_not_assumed(tmp_path):
    p = tmp_path / "input.csv"
    write_csv(p, [input_row(entry_timestamp_utc="2026-11-01T00:00:00+00:00",
                            original_deadline_utc="2026-11-02T06:00:00+00:00")], INPUT_FIELDS)
    trades = adapter.load_trades(p)
    assert trades[0]["max_holding_time_hours"] == 30.0


def test_bad_geometry_is_flagged():
    row = input_row(stop_loss="1.10500")  # stop above entry for a long
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        path = Path(f.name)
    write_csv(path, [row], INPUT_FIELDS)
    trades = adapter.load_trades(path)
    path.unlink()
    assert any("geometry" in e for e in trades[0]["_errors"])


def test_wrong_contract_version_is_flagged(tmp_path):
    p = tmp_path / "input.csv"
    write_csv(p, [input_row(measurement_contract_version="v1")], INPUT_FIELDS)
    trades = adapter.load_trades(p)
    assert any("measurement_contract_version" in e for e in trades[0]["_errors"])


def evidence_row(trade_id, t, bid, ask, tradeable="True"):
    return {"trade_id": trade_id, "run_id": "run-A", "pair": "EURUSD", "received_at_utc": t,
            "oanda_time_utc": t, "bid": str(bid), "ask": str(ask), "tradeable": tradeable,
            "window_start_utc": t, "nominal_deadline_utc": t}


def test_evidence_is_bucketed_by_trade_id(tmp_path):
    p = tmp_path / "evidence.csv"
    rows = [evidence_row("v2-001", "2026-10-21T01:01:13+00:00", 1.0996, 1.1000),
            evidence_row("v2-002", "2026-10-22T17:02:03+00:00", 0.99, 1.00)]
    write_csv(p, rows, EVIDENCE_FIELDS)
    by_trade = adapter.load_evidence_by_trade(p)
    assert set(by_trade.keys()) == {"v2-001", "v2-002"}
    assert len(by_trade["v2-001"]) == 1
    assert by_trade["v2-001"][0]["bid"] == 1.0996


def test_evidence_filter_skips_unwanted_trade_ids(tmp_path):
    p = tmp_path / "evidence.csv"
    rows = [evidence_row("v2-001", "2026-10-21T01:01:13+00:00", 1.0996, 1.1000),
            evidence_row("v2-002", "2026-10-22T17:02:03+00:00", 0.99, 1.00)]
    write_csv(p, rows, EVIDENCE_FIELDS)
    by_trade = adapter.load_evidence_by_trade(p, wanted_trade_ids={"v2-001"})
    assert set(by_trade.keys()) == {"v2-001"}


def test_evidence_malformed_row_is_dropped(tmp_path):
    p = tmp_path / "evidence.csv"
    rows = [evidence_row("v2-001", "2026-10-21T01:01:13+00:00", 1.0996, 1.1000),
            {**evidence_row("v2-001", "2026-10-21T01:01:18+00:00", 1.0996, 1.1000), "bid": "nope"}]
    write_csv(p, rows, EVIDENCE_FIELDS)
    by_trade = adapter.load_evidence_by_trade(p)
    assert len(by_trade["v2-001"]) == 1


def test_original_outcomes_matched_by_pair_and_entry_time(tmp_path):
    kpi_path = tmp_path / "kpi.json"
    import json
    kpi_path.write_text(json.dumps({"ledger": [
        {"executable": True, "pair": "USDCAD", "assumed_entry_time_utc": "2026-10-23T13:02:16.486494+00:00",
         "state": "time_exited", "exit_time_utc": "2026-10-25T21:04:56.913807+00:00",
         "exit_price": 1.40487, "r_multiple": -0.754},
    ]}))
    trades = [{"trade_id": "v2-003", "pair": "USDCAD", "assumed_entry_time_utc": "2026-10-23T13:02:16.486494+00:00"},
              {"trade_id": "v2-004", "pair": "EURUSD", "assumed_entry_time_utc": "2026-10-26T09:03:53.675377+00:00"}]
    outcomes = adapter.load_original_outcomes(kpi_path, trades)
    assert outcomes["v2-003"]["found"] is True
    assert outcomes["v2-003"]["r_multiple"] == -0.754
    assert outcomes["v2-004"]["found"] is False
