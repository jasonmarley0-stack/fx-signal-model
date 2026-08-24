"""Second reviewed evidence batch — pushes hand-reviewed raw evidence through
Signal Engine's manual review workflow: (match company if needed) -> map
SignalType -> approve -> publish.

Reviewed by reading all 135 unreviewed items in the queue as of 2026-08-24
(114 NORMALISED + 21 UNMATCHED). The large majority were rejected as
administrative noise per the established review principle — see chat and
setup/process_evidence_batch.py's docstring. Notably: the BoE feed (50
items) was almost entirely committee minutes, banknote-imagery panels, and
statistical notices; the SNB feed (20 items) carried titles only with no
article content, so nothing on it could be classified with real confidence;
the Fed feed (20 items) was almost entirely routine enforcement actions
against individual banks and bank-application approvals, not FOMC policy
outcomes. Rate HOLDS (BoE June/July, BoC April/June/July) were excluded per
the standing design decision (see SIGNAL_ENGINE_SETUP.md) — holds are
ambiguous and not currently modeled.

Two classes of item were rejected specifically for lacking a fitting
SignalType, not for being irrelevant — worth flagging for the catalog:
- UK retail sales (miss) and Canada retail sales (beat) — genuine hard
  economic data with a clear beat/miss, but there's no RETAIL_SALES
  SignalType; CONSUMER_CONFIDENCE_UP/DOWN is a sentiment-survey type and
  would be a stretch to use for actual spending data.
- This is the same shape of gap already flagged for PMI in
  SIGNAL_ENGINE_SETUP.md ("no PMI-specific PESTLE SignalType"). Composite
  PMI *was* mapped to CONSUMER_CONFIDENCE_UP/DOWN here (as the first batch
  already did for French business confidence) since PMI is itself a
  business-sentiment survey, a much closer conceptual fit than retail
  sales is.

Usage:
    export SIGNAL_ENGINE_BASE_URL=http://127.0.0.1:8000
    python3 setup/process_evidence_batch_2.py
"""
from __future__ import annotations
import os
import sys
import httpx

BASE_URL = os.environ.get("SIGNAL_ENGINE_BASE_URL", "http://127.0.0.1:8000")
ACTOR = "claude-review-2026-08-24"

COMPANY_ID = {"GBP": 1, "USD": 2, "EUR": 3, "JPY": 4, "CHF": 5, "AUD": 6, "CAD": 7, "NZD": 8}
SIGNAL_TYPE_ID = {
    "CONSUMER_CONFIDENCE_UP": 41,
    "CONSUMER_CONFIDENCE_DOWN": 42,
    "FINANCIAL_INFRA_POSITIVE": 44,
}

# (evidence_id, currency, signal_type_code, confidence, already_matched, notes)
BATCH = [
    (140, "AUD", "CONSUMER_CONFIDENCE_DOWN", 0.5, False,
     "Aug flash composite PMI slipped to 52.5 from 53.2 — still expansion but growth pace "
     "moderating; input price inflation reaccelerated for the first time in 3 months."),
    (137, "JPY", "CONSUMER_CONFIDENCE_UP", 0.65, False,
     "Flash manufacturing PMI shows strongest growth since February, near-record selling "
     "price inflation, sustained employment rise — explicitly strengthens the case for a "
     "BoJ September hike."),
    (128, "EUR", "CONSUMER_CONFIDENCE_DOWN", 0.55, False,
     "France flash composite PMI 48.8 vs 49.5 expected — contraction, missed forecast; "
     "services notably weak (48.4 vs 49.8 exp), manufacturing beat but not enough to offset."),
    (126, "GBP", "CONSUMER_CONFIDENCE_UP", 0.6, False,
     "UK flash composite PMI 52.5 vs 51.6 expected — beat; chief business economist "
     "commentary explicitly cites picked-up pace, ~0.3% Q3 growth signal."),
    (125, "EUR", "CONSUMER_CONFIDENCE_UP", 0.65, False,
     "Eurozone-wide (not single-country) flash composite PMI 52.1 vs 51.7 expected — broad "
     "beat across services/manufacturing despite France/Germany softness, described in the "
     "source as a surprise beat."),
    (91, "CAD", "FINANCIAL_INFRA_POSITIVE", 0.45, True,
     "BoC joins BIS Project Agora, exploring tokenization to improve wholesale cross-border "
     "payments — supportive fintech/payments infrastructure development, pilot/exploratory "
     "stage so moderate-low confidence, same category as the already-published BoC-PBoC "
     "swap-agreement item."),
]


def main():
    client = httpx.Client(base_url=BASE_URL, timeout=30)
    for evidence_id, currency, code, confidence, already_matched, notes in BATCH:
        company_id = COMPANY_ID[currency]
        signal_type_id = SIGNAL_TYPE_ID[code]
        print(f"\n=== Evidence #{evidence_id} -> {currency} / {code} ===")

        if not already_matched:
            r = client.post(f"/raw-evidence/{evidence_id}/match-company",
                             json={"actor": ACTOR, "notes": notes, "company_id": company_id})
            if r.status_code != 200:
                print(f"  match-company FAILED: {r.status_code} {r.text}")
                continue
            print(f"  matched to {currency} (company_id={company_id})")
        else:
            print(f"  already matched to {currency} (company_id={company_id}) — skipping match-company")

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
    print("  (or check the live dashboard — AUD/JPY/EUR/GBP/CAD should show non-zero PESTLE now)")


if __name__ == "__main__":
    if not BASE_URL:
        sys.exit("SIGNAL_ENGINE_BASE_URL not set")
    main()
