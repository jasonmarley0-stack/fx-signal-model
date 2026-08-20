"""Posts upcoming economic-calendar events into Signal Engine as
SCHEDULED_HIGH_IMPACT_EVENT signals, so the dashboard's "Upcoming" section
can show them (see src/calendar_events.py for the parsing side).

Data source: since Investing.com has no public API/RSS (confirmed via
WebFetch during setup), this JSON file is populated by a Claude session
using WebFetch against investing.com's calendar page — not scraped
automatically by this script, and not something a plain cron job on your
machine can do reliably or within the site's terms. Ask in chat to refresh
setup/economic_calendar.json with the next few days' high-impact events,
then run this script to post them.

Usage:
    python3 seed_calendar_events.py [--base-url http://127.0.0.1:8000]
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
import httpx

SETUP_DIR = Path(__file__).parent


def get_company_id(client: httpx.Client, currency_name: str) -> int | None:
    resp = client.get("/companies", params={"name": currency_name})
    if resp.status_code != 200:
        return None
    items = resp.json()
    items = items.get("items", items) if isinstance(items, dict) else items
    for c in items:
        if isinstance(c, dict) and c.get("name") == currency_name:
            return c.get("id")
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()

    events = json.loads((SETUP_DIR / "economic_calendar.json").read_text())
    print(f"Posting {len(events)} upcoming calendar event(s) to {args.base_url}")

    try:
        with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
            for ev in events:
                company_id = get_company_id(client, ev["currency"])
                if company_id is None:
                    print(f"  SKIP: currency '{ev['currency']}' not found — run seed_signal_engine.py first")
                    continue
                payload = {
                    "company_id": company_id,
                    "signal_type_code": "SCHEDULED_HIGH_IMPACT_EVENT",
                    "description": f"[SCHEDULED {ev['scheduled_at_utc']}] {ev['title']}",
                    "confidence": 0.9,  # confidence in the calendar entry itself, not a directional prediction
                    "source_credibility": ev.get("source_credibility", 60),
                    "observed_at": ev.get("posted_at_utc") or ev["scheduled_at_utc"],
                    "source_name": ev.get("source_name", "Investing.com economic calendar"),
                }
                resp = client.post("/signals", json=payload)
                if resp.status_code in (200, 201):
                    print(f"  posted: {ev['currency']} — {ev['title'][:60]} @ {ev['scheduled_at_utc']}")
                else:
                    print(f"  FAILED ({resp.status_code}) for '{ev['title'][:50]}': {resp.text[:300]}")
    except httpx.ConnectError:
        print(f"\nCould not connect to {args.base_url} — is Signal Engine running?")
        sys.exit(1)

    print("\nDone. src/calendar_events.py will surface these in the dashboard's "
          "Upcoming section once they're within the within_hours window.")
