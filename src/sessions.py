"""FX trading session windows in UTC/GMT, and mapping of which session is
primary for a given pair. Used to attach an explicit signal-valid time
window to every trade signal — a signal generated off the London opening
range shouldn't be read as "still valid" at the NY close.

Session opens/closes are defined in each market's LOCAL time and converted
to UTC per specific date via zoneinfo, so daylight saving is handled
correctly automatically. (An earlier version hardcoded fixed UTC times,
which was wrong for exactly the winter months this project's first
backtests ran on — London's real local open is 08:00 year-round, which is
07:00 UTC in BST but 08:00 UTC in GMT/winter; the old fixed "07:00 UTC"
was quietly using the wrong hour for Nov/Dec/Jan data.)

All times are ultimately expressed in UTC, which the dashboard labels
"GMT" per Jase's preference — outside British Summer Time these are the
same clock; during BST, UTC is what's used internally even though the
displayed London local time is UTC+1.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, time as dtime
from zoneinfo import ZoneInfo

UTC = timezone.utc


@dataclass
class Session:
    name: str
    tz_name: str          # IANA timezone of the market itself
    open_local: dtime     # session open, in that market's local time
    close_local: dtime    # session close, in that market's local time


SESSIONS = {
    "sydney":   Session("Sydney", "Australia/Sydney", dtime(8, 0), dtime(17, 0)),
    "tokyo":    Session("Tokyo", "Asia/Tokyo", dtime(9, 0), dtime(18, 0)),
    "london":   Session("London", "Europe/London", dtime(8, 0), dtime(17, 0)),
    "new_york": Session("New York", "America/New_York", dtime(8, 0), dtime(17, 0)),
}

# Which session's opening range/trend context is primary for each pair.
# (Majors are typically most active in the session(s) touching their base
# or quote currency's home market; London/NY overlap dominates EUR/GBP/USD
# crosses, Tokyo dominates JPY, Sydney dominates AUD/NZD.)
PRIMARY_SESSION = {
    "EURUSD": "london", "GBPUSD": "london", "EURGBP": "london",
    "USDJPY": "tokyo", "AUDUSD": "sydney", "NZDUSD": "sydney",
    "USDCAD": "new_york", "USDCHF": "london", "GBPJPY": "tokyo",
}

ORB_WINDOW_MINUTES = 30
# How long a signal stays "live" after it's generated before it should be
# considered stale and re-evaluated — kept inside the same session by default.
DEFAULT_SIGNAL_VALIDITY_MINUTES = 90


def session_open_datetime(session_key: str, reference_date) -> datetime:
    """UTC open time for this session on this specific date, DST-correct."""
    session = SESSIONS[session_key]
    local_open = datetime.combine(reference_date, session.open_local, tzinfo=ZoneInfo(session.tz_name))
    return local_open.astimezone(UTC)


def session_close_datetime(session_key: str, reference_date) -> datetime:
    session = SESSIONS[session_key]
    local_close = datetime.combine(reference_date, session.close_local, tzinfo=ZoneInfo(session.tz_name))
    close_dt = local_close.astimezone(UTC)
    open_dt = session_open_datetime(session_key, reference_date)
    if close_dt <= open_dt:  # session wraps past midnight UTC
        close_dt += timedelta(days=1)
    return close_dt


def signal_window(pair: str, generated_at: datetime, validity_minutes: int = DEFAULT_SIGNAL_VALIDITY_MINUTES) -> dict:
    """Returns the explicit GMT validity window for a signal generated on `pair`
    at `generated_at` (must be tz-aware UTC)."""
    session_key = PRIMARY_SESSION.get(pair, "london")
    session = SESSIONS[session_key]
    session_close = session_close_datetime(session_key, generated_at.date())

    valid_until = min(generated_at + timedelta(minutes=validity_minutes), session_close)

    return {
        "session": session.name,
        "generated_at_utc": generated_at,
        "valid_until_utc": valid_until,
        "generated_at_gmt_str": generated_at.strftime("%H:%M GMT, %d %b %Y"),
        "valid_until_gmt_str": valid_until.strftime("%H:%M GMT, %d %b %Y"),
    }
