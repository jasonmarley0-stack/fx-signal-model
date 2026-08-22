"""Registers each *verified* feed in setup/rss_feeds_financial.json as a
Signal Engine EvidenceSource (RSS/Atom connector), then runs one ingestion
pass per source so real articles land in the raw-evidence review queue.

This does NOT create any Signals — it only gets real evidence flowing in.
Turning a piece of raw evidence into a scored PESTLE Signal (choosing which
SignalType it maps to, e.g. RATE_HIKE) is a separate manual review step via
Signal Engine's /raw-evidence/{id}/approve and /publish endpoints — see
SIGNAL_ENGINE_SETUP.md and the chat discussion on why that's deliberately
not automated here (no substring/AI auto-classification, per Signal
Engine's own design principles).

Usage:
    export SIGNAL_ENGINE_BASE_URL=http://127.0.0.1:8000
    python3 setup/register_rss_sources.py
"""
from __future__ import annotations
import json
import os
import re
import sys
from pathlib import Path
import httpx

BASE_URL = os.environ.get("SIGNAL_ENGINE_BASE_URL", "http://127.0.0.1:8000")
FEEDS_PATH = Path(__file__).parent / "rss_feeds_financial.json"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    return f"rss_{slug}"[:100]


def main():
    feeds = json.loads(FEEDS_PATH.read_text())
    verified = [f for f in feeds if f.get("verified") and f.get("url")]
    print(f"{len(verified)} of {len(feeds)} feeds are verified with a real URL — registering those.\n")

    client = httpx.Client(base_url=BASE_URL, timeout=30)
    registered = []

    for feed in verified:
        code = slugify(feed["name"])
        currency = feed["currency"]
        entity_names = [currency] if currency != "ALL" else []
        payload = {
            "code": code,
            "name": feed["name"],
            "description": f"Financial RSS/Atom feed for {feed['currency']} PESTLE evidence — {feed['name']}",
            "source_class": "central_bank" if feed["credibility"] >= 95 else "newswire",
            "connector_type": "rss_atom",
            "base_url": feed["url"],
            "enabled": True,
            "automatic_publication_enabled": False,
            "default_source_credibility": feed["credibility"],
            "default_confidence": 0.75,
            "polling_interval_minutes": 60,
            "configuration": {
                "feed_url": feed["url"],
                "entity_names": entity_names,
                "mapping_key": currency if currency != "ALL" else None,
            },
        }
        resp = client.post("/evidence-sources", json=payload)
        if resp.status_code == 409:
            print(f"  {code}: already registered, skipping creation")
            existing = client.get("/evidence-sources", params={"enabled": True}).json()
            match = next((s for s in existing if s["code"] == code), None)
            if match:
                registered.append((code, match["id"]))
            continue
        if resp.status_code != 201:
            print(f"  {code}: FAILED to register — {resp.status_code} {resp.text}")
            continue
        source_id = resp.json()["id"]
        print(f"  {code}: registered (id={source_id})")
        registered.append((code, source_id))

    print(f"\n=== Running ingestion for {len(registered)} source(s) ===")
    for code, source_id in registered:
        resp = client.post(f"/evidence-sources/{source_id}/run")
        if resp.status_code != 200:
            print(f"  {code}: ingestion FAILED — {resp.status_code} {resp.text}")
            continue
        run = resp.json()
        print(f"  {code}: run {run.get('status', '?')} — "
              f"{run.get('records_ingested', run.get('record_count', '?'))} record(s)")

    print("\nDone. Check what came in with:")
    print("  curl -s $SIGNAL_ENGINE_BASE_URL/raw-evidence | python3 -m json.tool | head -60")
    print("Real evidence is now sitting in the review queue — it won't affect PESTLE scores")
    print("until each item is approved and published with a chosen SignalType.")


if __name__ == "__main__":
    if not BASE_URL:
        sys.exit("SIGNAL_ENGINE_BASE_URL not set")
    main()
