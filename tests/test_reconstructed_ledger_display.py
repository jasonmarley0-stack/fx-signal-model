"""Display overlay only: never edits or reclassifies original paper results."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DASHBOARD_USER"] = "testuser"
os.environ["DASHBOARD_PASSWORD"] = "testpass123"
import dashboard_server as ds
from fastapi.testclient import TestClient


def case():
    health = {"state": "ok", "generated_at_utc": "2025-01-06T09:00:00+00:00",
              "run_identity": {"run_id": "run-A", "source_hash": "hash-A"},
              "service": {"status": "active"}, "recording_health": "healthy"}
    rows = []
    for n, pair, direction in [(1, "USDJPY", "long"), (2, "USDJPY", "short"),
                               (3, "USDCAD", "short"), (4, "EURUSD", "short")]:
        rows.append({"decision_id": f"d{n}", "actual_recording_time_utc": f"2025-01-0{n}T09:00:00+00:00",
                     "pair": pair, "direction": direction, "executable": True, "assumed_entry_price": 1.1,
                     "assumed_entry_time_utc": f"2025-01-0{n}T09:00:05+00:00", "max_holding_time_hours": 30,
                     "state": "incomplete_coverage", "exit_time_utc": None, "exit_price": None,
                     "r_multiple": None, "caveats": ["Generic paper disclaimer repeated in the old UI."]})
    rows[2].update(state="time_exited", exit_time_utc="2025-01-05T15:00:00+00:00", exit_price=1.11, r_multiple=-.754)
    kpi = {"state": "ok", "run_id": "run-A", "generated_at_utc": "2025-01-06T09:00:00+00:00", "ledger": rows,
           "kpi": {"counts": {"eligible_alerts": 4, "entered": 4, "completed": 1, "unknown_total": 3,
                              "suppressed_existing_position": 0, "pending_open": 0},
                   "avg_net_r_per_completed_trade": {"value": -.754, "denominator": 1}}}
    audited = []
    for row, state, r in [(rows[0], "stopped", -1), (rows[1], "targeted", 1.02), (rows[3], "unknown", None)]:
        audited.append({"decision_id": row["decision_id"], "pair": row["pair"],
                        "assumed_entry_time_utc": row["assumed_entry_time_utc"], "state": state, "r_multiple": r,
                        "exit_time_utc": "2025-01-05T15:00:00+00:00" if r is not None else None,
                        "exit_price": 1.11 if r is not None else None, "as_of_utc": "2025-01-06T09:00:00+00:00",
                        "reason": "Missing candle evidence." if r is None else None})
    evidence = {"state": "ok", "run_id": "run-A", "source_hash": "hash-A", "live_kpi_unchanged": True,
                "measurement_contract": "supplementary_gap_evidence_v1", "ledger": audited}
    return health, kpi, evidence


def test_shows_recovered_outcomes_without_mutating_original_states_or_counts():
    health, kpi, audit = case()
    before = copy.deepcopy((health, kpi, audit))
    view = ds.research_ledger_display(health, kpi, audit)
    assert [r["display_result"]["state"] for r in view["ledger"]] == ["stopped", "targeted", "time_exited", "incomplete_coverage"]
    assert view["display_meta"]["shown_determined"] == 3
    assert view["display_meta"]["shown_reconstructed"] == 2
    assert view["display_meta"]["shown_undetermined"] == 1
    assert view["ledger"][0]["state"] == "incomplete_coverage"
    assert view["kpi"] == kpi["kpi"] and view["kpi"]["counts"]["completed"] == 1
    assert (health, kpi, audit) == before


@pytest.mark.parametrize("field,value", [("run_id", "another-run"), ("source_hash", "another-hash"),
    ("measurement_contract", "another-contract"), ("live_kpi_unchanged", False), ("state", "error")])
def test_foreign_or_unavailable_evidence_cannot_replace_a_result(field, value):
    health, kpi, audit = case()
    audit[field] = value
    view = ds.research_ledger_display(health, kpi, audit)
    assert view["display_meta"]["shown_reconstructed"] == 0
    assert view["ledger"][0]["display_result"]["state"] == "incomplete_coverage"


@pytest.mark.parametrize("change", [{"pair": "GBPUSD"}, {"decision_id": "wrong"},
    {"assumed_entry_time_utc": "2025-01-01T09:00:06+00:00"}, {"r_multiple": float("nan")},
    {"exit_price": None}, {"exit_time_utc": "2024-01-01T09:00:00+00:00"},
    {"as_of_utc": "2099-01-01T09:00:00+00:00"}])
def test_mismatched_or_invalid_audit_row_is_not_used(change):
    health, kpi, audit = case()
    audit["ledger"][0].update(change)
    view = ds.research_ledger_display(health, kpi, audit)
    assert view["ledger"][0]["display_result"]["source"] == "live"


def test_duplicate_identity_is_not_arbitrarily_selected():
    health, kpi, audit = case()
    audit["ledger"].append(copy.deepcopy(audit["ledger"][0]))
    assert ds.research_ledger_display(health, kpi, audit)["ledger"][0]["display_result"]["source"] == "live"


def test_existing_live_completion_takes_priority():
    health, kpi, audit = case()
    wrong = {**audit["ledger"][0], "decision_id": "d3", "pair": "USDCAD",
             "assumed_entry_time_utc": kpi["ledger"][2]["assumed_entry_time_utc"], "r_multiple": 100}
    audit["ledger"].append(wrong)
    result = ds.research_ledger_display(health, kpi, audit)["ledger"][2]["display_result"]
    assert result["source"] == "live" and result["r_multiple"] == -.754


def test_authenticated_route_only_reads_snapshots_and_preserves_live_api(tmp_path, monkeypatch):
    health, kpi, audit = case()
    paths = {}
    for name, payload in [("RESEARCH_HEALTH_V2_PATH", health), ("RESEARCH_KPI_V2_PATH", kpi),
                          ("RESEARCH_EVIDENCE_V2_PATH", audit)]:
        p = tmp_path / (name + ".json")
        p.write_text(json.dumps(payload))
        paths[name] = p
        monkeypatch.setattr(ds, name, p)
    before = {p: p.read_bytes() for p in paths.values()}
    client = TestClient(ds.app)
    assert client.get("/research/ledger_v2").status_code == 401
    response = client.get("/research/ledger_v2", auth=("testuser", "testpass123"))
    assert response.status_code == 200 and response.json()["display_meta"]["shown_reconstructed"] == 2
    assert client.get("/research/kpi_v2", auth=("testuser", "testpass123")).json() == kpi
    assert {p: p.read_bytes() for p in paths.values()} == before
    paths["RESEARCH_EVIDENCE_V2_PATH"].unlink()
    missing = client.get("/research/ledger_v2", auth=("testuser", "testpass123")).json()
    assert missing["display_meta"]["evidence_state"] == "unavailable"
    assert missing["display_meta"]["shown_reconstructed"] == 0


def test_actual_javascript_initial_manual_and_automatic_rendering():
    health, kpi, audit = case()
    view = ds.research_ledger_display(health, kpi, audit)
    page = ds.render_page(None, None, None, {"state": "not_published"}, {"state": "not_published"}, health, view)
    data = re.search(r'<script id="research-v2-data" type="application/json">(.*?)</script>', page, re.S).group(1)
    start = page.index("const researchV2DataEl =")
    script = page[start:page.index("</script>", start)]
    harness = r'''
const vm = require('vm'), fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const els = {}, timers = [], routes = [];
for (const id of ['research-v2-data','research-v2-health-container','research-v2-health-freshness',
  'research-v2-kpi-container','research-v2-kpi-freshness','research-v2-ledger-body',
  'research-v2-evidence-note','research-v2-kpi-refresh']) {
  els[id] = {innerHTML:'',textContent:'',disabled:false,listeners:{},addEventListener(n,fn){this.listeners[n]=fn;}};
}
els['research-v2-data'].textContent = input.data;
let reply = JSON.parse(input.data).kpi;
const context = {document:{getElementById:id=>els[id]}, Date, Number, Set, console,
  setInterval:(fn,ms)=>timers.push({fn,ms}),
  fetch:url=>{routes.push(url);return Promise.resolve({ok:true,json:()=>Promise.resolve(reply)});}};
vm.runInNewContext(input.script, context);
const first = els['research-v2-ledger-body'].innerHTML;
(async()=>{
  reply = JSON.parse(JSON.stringify(reply));
  reply.ledger[0].display_result.r_multiple = -.9;
  els['research-v2-kpi-refresh'].listeners.click();
  await new Promise(setImmediate);
  const manual = els['research-v2-ledger-body'].innerHTML;
  reply.ledger[0].display_result.r_multiple = -.8;
  timers.find(t=>t.ms===300000).fn();
  await new Promise(setImmediate);
  console.log(JSON.stringify({first, manual, automatic:els['research-v2-ledger-body'].innerHTML,
    routes, disabled:els['research-v2-kpi-refresh'].disabled, note:els['research-v2-evidence-note'].textContent}));
})();
'''
    process = subprocess.run(["node", "-e", harness], input=json.dumps({"data": data, "script": script}),
                             text=True, capture_output=True, check=True)
    result = json.loads(process.stdout)
    assert "Reconstructed" in result["first"] and "Stopped" in result["first"] and "Targeted" in result["first"]
    assert "-1.00R" in result["first"] and "+1.02R" in result["first"]
    assert "Unresolved — evidence gap" in result["first"] and "<details" in result["first"]
    assert "-0.90R" in result["manual"] and "-0.80R" in result["automatic"]
    assert result["routes"] == ["/research/ledger_v2", "/research/ledger_v2"]
    assert not result["disabled"] and "3 determined (2 reconstructed)" in result["note"]
