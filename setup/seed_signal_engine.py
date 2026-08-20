"""One-time seed script: POSTs the 8 currency Companies and ~24 PESTLE
SignalTypes into a running Signal Engine instance via its REST API.

Keeps the two codebases fully separate (per the architecture decision in
PESTLE_SIGNAL_ENGINE_INTEGRATION.md) — this never imports Signal Engine's
Python code, only talks to its HTTP API, the same way signal_engine_client.py
does at runtime.

Usage:
    python3 seed_signal_engine.py [--base-url http://127.0.0.1:8000]

Idempotent-ish: if a Company or SignalType already exists, Signal Engine's
API will reject the duplicate (unique name/code) — this script reports
those as "already exists" rather than failing, so it's safe to re-run.
"""
import argparse
import json
import sys
from pathlib import Path
import httpx

SETUP_DIR = Path(__file__).parent


def seed_companies(client: httpx.Client) -> None:
    companies = json.loads((SETUP_DIR / "seed_currencies.json").read_text())
    print(f"\n=== Seeding {len(companies)} currency Companies ===")
    for company in companies:
        resp = client.post("/companies", json=company)
        if resp.status_code in (200, 201):
            print(f"  created: {company['name']}")
        elif resp.status_code in (400, 409, 422):
            print(f"  already exists (or rejected): {company['name']} — {resp.status_code}: {resp.text[:150]}")
        else:
            print(f"  UNEXPECTED {resp.status_code} for {company['name']}: {resp.text[:200]}")


def seed_signal_types(client: httpx.Client) -> None:
    signal_types = json.loads((SETUP_DIR / "seed_pestle_signal_types.json").read_text())
    print(f"\n=== Seeding {len(signal_types)} PESTLE SignalTypes ===")
    for st in signal_types:
        # pestle_category is FX-side metadata only (see signal_engine_client.py's
        # PESTLE_SIGNAL_POLARITY table) — Signal Engine's own schema doesn't have
        # this field and its existing `category` enum is used unmodified below,
        # so nothing about Signal Engine itself needs to change.
        payload = {k: v for k, v in st.items() if k != "pestle_category"}
        resp = client.post("/signal-types", json=payload)
        if resp.status_code in (200, 201):
            print(f"  created: {st['code']} (category={st['category']}, pestle={st['pestle_category']})")
        elif resp.status_code in (400, 409, 422):
            print(f"  already exists (or rejected): {st['code']} — {resp.status_code}: {resp.text[:200]}")
        else:
            print(f"  UNEXPECTED {resp.status_code} for {st['code']}: {resp.text[:200]}")


def verify(client: httpx.Client) -> None:
    print("\n=== Verification ===")
    companies_resp = client.get("/companies")
    signal_types_resp = client.get("/signal-types")
    print(f"GET /companies -> {companies_resp.status_code}")
    print(f"GET /signal-types -> {signal_types_resp.status_code}")
    if companies_resp.status_code == 200:
        items = companies_resp.json()
        items = items.get("items", items) if isinstance(items, dict) else items
        currencies_found = {c.get("name") for c in items if isinstance(c, dict)}
        expected = {"GBP", "USD", "EUR", "JPY", "CHF", "AUD", "CAD", "NZD"}
        missing = expected - currencies_found
        if missing:
            print(f"  WARNING: currencies not found in Signal Engine: {missing}")
        else:
            print("  all 8 currencies present")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    print(f"Seeding Signal Engine at {args.base_url}")
    print("Make sure `uvicorn app.main:app --reload` is running in another terminal first.")
    print("This script only calls Signal Engine's existing REST API — it makes "
          "no changes to your Signal Engine codebase.")

    try:
        with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
            health = client.get("/health")
            if health.status_code != 200:
                print(f"WARNING: /health returned {health.status_code}, continuing anyway...")
            seed_companies(client)
            seed_signal_types(client)
            verify(client)
    except httpx.ConnectError:
        print(f"\nCould not connect to {args.base_url} — is Signal Engine running? "
              f"Start it with: uvicorn app.main:app --reload")
        sys.exit(1)

    print("\nDone. Next: SIGNAL_ENGINE_BASE_URL in your environment (or fx-signal-model's "
          "src/pestle/signal_engine_client.py default) should point at this URL, then run "
          "src/pestle/pestle_scorer.py against real data instead of the mock fixtures.")
