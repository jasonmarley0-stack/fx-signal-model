"""Pushes a hand-reviewed batch of raw evidence through Signal Engine's
manual review workflow: match company -> map SignalType -> approve ->
publish. Each classification below was made by reading the actual article
headline/content (see chat) — this script is NOT auto-classification, it's
just the mechanical step of applying decisions already made by a human
(well, Claude, reviewing on Jase's behalf) via the API instead of clicking
through the UI 6 times.

Only run this for evidence IDs you've actually reviewed and are confident
about — publish is meant to be a considered decision, not a rubber stamp.

Usage:
    export SIGNAL_ENGINE_BASE_URL=http://127.0.0.1:8000
    python3 setup/process_evidence_batch.py
"""
from __future__ import annotations
import os
import sys
import httpx

BASE_URL = os.environ.get("SIGNAL_ENGINE_BASE_URL", "http://127.0.0.1:8000")
ACTOR = "claude-review-2026-08-22"

COMPANY_ID = {"GBP": 1, "USD": 2, "EUR": 3, "JPY": 4, "CHF": 5, "AUD": 6, "CAD": 7, "NZD": 8}
SIGNAL_TYPE_ID = {
    "TRADE_BALANCE_WORSEN": 35,
    "CONSUMER_CONFIDENCE_UP": 41,
    "FINANCIAL_INFRA_POSITIVE": 44,
    "CLIMATE_ENERGY_POLICY_POSITIVE": 48,
}

# (evidence_id, currency, signal_type_code, confidence, notes)
BATCH = [
    (141, "NZD", "TRADE_BALANCE_WORSEN", 0.9,
     "July trade balance NZ$-1.95bn vs prior +23mn; annual deficit widened to -5.24bn from -3.75bn."),
    (139, "GBP", "CONSUMER_CONFIDENCE_UP", 0.85,
     "GfK Consumer Confidence Index -14 in Aug vs -18 forecast, 2-year high; broad-based improvement."),
    (121, "EUR", "CONSUMER_CONFIDENCE_UP", 0.8,
     "EU consumer confidence -15.5 vs -16.3 expected — beat forecast."),
    (130, "EUR", "CONSUMER_CONFIDENCE_UP", 0.6,
     "French business confidence extends recovery into August (France-specific, moderate confidence for EUR-wide read)."),
    (116, "CAD", "FINANCIAL_INFRA_POSITIVE", 0.5,
     "BoC renews bilateral local-currency swap agreement with People's Bank of China — supportive payments infrastructure development."),
    (31, "EUR", "CLIMATE_ENERGY_POLICY_POSITIVE", 0.5,
     "ECB extends use of climate factors in Eurosystem collateral framework to non-financial corporate collateral."),
]


def main():
    client = httpx.Client(base_url=BASE_URL, timeout=30)
    for evidence_id, currency, code, confidence, notes in BATCH:
        company_id = COMPANY_ID[currency]
        signal_type_id = SIGNAL_TYPE_ID[code]
        print(f"\n=== Evidence #{evidence_id} -> {currency} / {code} ===")

        r = client.post(f"/raw-evidence/{evidence_id}/match-company",
                         json={"actor": ACTOR, "notes": notes, "company_id": company_id})
        if r.status_code != 200:
            print(f"  match-company FAILED: {r.status_code} {r.text}")
            continue
        print(f"  matched to {currency} (company_id={company_id})")

        r = client.post(f"/raw-evidence/{evidence_id}/map-signal-type",
                         json={"actor": ACTOR, "notes": notes, "signal_type_id": signal_type_id,
                               "confidence": confidence})
        if r.status_code != 200:
            print(f"  map-signal-type FAILED: {r.status_code} {r.text}")
            continue
        print(f"  mapped to {code} (confidence={confidence})")

        r = client.post(f"/raw-evidence/{evidence_id}/approve", json={"actor": ACTOR, "notes": notes})
        if r.status_code != 200:
            print(f"  approve FAILED: {r.status_code} {r.text}")
            continue
        print("  approved")

        r = client.post(f"/raw-evidence/{evidence_id}/publish", json={"actor": ACTOR})
        if r.status_code != 200:
            print(f"  publish FAILED: {r.status_code} {r.text}")
            continue
        result = r.json()
        print(f"  published — status={result.get('status')}, published_signal_id={result.get('published_signal_id')}")

    print("\nDone. Verify PESTLE scores moved with:")
    print("  curl -s $SIGNAL_ENGINE_BASE_URL/companies | python3 -m json.tool")
    print("  (or just check the live dashboard — GBP/EUR/CAD/NZD should show non-zero PESTLE now)")


if __name__ == "__main__":
    if not BASE_URL:
        sys.exit("SIGNAL_ENGINE_BASE_URL not set")
    main()
