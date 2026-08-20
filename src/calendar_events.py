"""Parses SCHEDULED_HIGH_IMPACT_EVENT signals (posted via
setup/seed_calendar_events.py) back into upcoming-event objects for the
dashboard's "Upcoming" section.

Deliberately separate from pestle_scorer.py — these are forward-looking risk
flags, not scored evidence. See setup/seed_pestle_signal_types.json's
SCHEDULED_HIGH_IMPACT_EVENT entry for why it's excluded from
PESTLE_SIGNAL_POLARITY.
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from pestle.signal_engine_client import SignalEngineClient

SCHEDULED_PREFIX_RE = re.compile(r"^\[SCHEDULED\s+([0-9T:\-+Z]+)\]\s*(.*)$")


@dataclass
class UpcomingEvent:
    currency: str
    scheduled_at: datetime
    title: str
    raw_description: str


def _parse_row(currency: str, description: str) -> UpcomingEvent | None:
    match = SCHEDULED_PREFIX_RE.match(description)
    if not match:
        return None
    try:
        scheduled_at = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
    except ValueError:
        return None
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    return UpcomingEvent(currency=currency, scheduled_at=scheduled_at, title=match.group(2), raw_description=description)


def upcoming_events_for_currency(client: SignalEngineClient, currency: str, within_hours: int = 48,
                                  lookback_hours_for_fetch: int = 24 * 14) -> list[UpcomingEvent]:
    """Events posted (as evidence rows) within the last `lookback_hours_for_fetch`
    whose SCHEDULED time is still in the future and within `within_hours` from now."""
    now = datetime.now(timezone.utc)
    signals = client.fetch_signals(currency, lookback_hours=lookback_hours_for_fetch)
    out = []
    for sig in signals:
        if sig.signal_type_code != "SCHEDULED_HIGH_IMPACT_EVENT":
            continue
        event = _parse_row(currency, sig.description)
        if event is None:
            continue
        delta_hours = (event.scheduled_at - now).total_seconds() / 3600
        if 0 <= delta_hours <= within_hours:
            out.append(event)
    return sorted(out, key=lambda e: e.scheduled_at)


def upcoming_events_for_pair(client: SignalEngineClient, pair: str, within_hours: int = 48) -> list[UpcomingEvent]:
    base, quote = pair[:3], pair[3:]
    events = upcoming_events_for_currency(client, base, within_hours) + upcoming_events_for_currency(client, quote, within_hours)
    return sorted(events, key=lambda e: e.scheduled_at)
