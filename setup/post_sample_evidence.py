"""Posts the real, sourced sample evidence in seed_sample_evidence.json into a
running Signal Engine instance as published Signals, so the PESTLE pipeline
can be tested end-to-end against genuine (not fabricated) data.

Payload shape confirmed against the running instance's own
`/openapi.json` -> `components.schemas.SignalCreate`: `company_id`,
`signal_type_code`, `description`, `confidence` (0-1 float),
`source_credibility` (0-100 int), and `observed_at` (ISO datetime) are
required; `source_name`/`source_url` are optional.

Usage:
    python3 post_sample_evidence.py [--base-url http://127.0.0.1:8000]
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import httpx

SETUP_DIR = Path(__file__).parent


def get_company_id(client: httpx.Client, currency_name: str) -> int | None:
    resp = client.get("/companies")
    if resp.status_code != 200:
        return None
    items = resp.json()
    items = items.get("items", items) if isinstance(items, dict) else items
    for c in items:
        if isinstance(c, dict) and c.get("name") == currency_name:
            return c.get("id")
    return None


def build_payload(entry: dict, company_id: int) -> dict:
    # Schema is SignalCreate (confirmed via /openapi.json): company_id,
    # signal_type_code, description, confidence (0-1 float), source_credibility
    # (0-100 int), observed_at (ISO datetime) are required; source_name/source_url
    # are optional. There's no separate headline/summary — folded into description.
    return {
        "company_id": company_id,
        "signal_type_code": entry["signal_type_code"],
        "description": f"{entry['headline']} — {entry['summary']}",
        "confidence": entry["confidence"],
        "source_credibility": entry["source_credibility"],
        "observed_at": f"{entry['event_date']}T00:00:00Z",
        "source_name": entry.get("source_name"),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    entries = json.loads((SETUP_DIR / "seed_sample_evidence.json").read_text())
    print(f"Posting {len(entries)} real evidence item(s) to {args.base_url}")
    print("(Run setup/seed_signal_engine.py first if you haven't — this needs "
          "the currency Companies and PESTLE SignalTypes to already exist.)\n")

    try:
        with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
            for entry in entries:
                company_id = get_company_id(client, entry["company_name"])
                if company_id is None:
                    print(f"  SKIP: company '{entry['company_name']}' not found — "
                          f"run seed_signal_engine.py first")
                    continue
                payload = build_payload(entry, company_id)
                resp = client.post("/signals", json=payload)
                if resp.status_code in (200, 201):
                    print(f"  posted: {entry['headline'][:70]}")
                else:
                    print(f"  FAILED ({resp.status_code}) for '{entry['headline'][:50]}': "
                          f"{resp.text[:300]}")
    except httpx.ConnectError:
        print(f"\nCould not connect to {args.base_url} — is Signal Engine running?")
        sys.exit(1)

    print("\nDone. Next: point fx-signal-model's SIGNAL_ENGINE_BASE_URL at this "
          "instance and re-run src/pestle/pestle_scorer.py / dashboard_data.py "
          "to see the real (non-mock) PESTLE score for EUR pick up this signal.")
