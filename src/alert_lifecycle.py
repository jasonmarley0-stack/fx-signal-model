"""Prospective alert lifecycle: the record a subscriber's alert actually
publishes, kept immutable and append-only, so `alert_scorer.py` can later
score exactly what was published rather than re-deriving it from a
snapshot-in-time signal log. See IMPLEMENTATION_NOTE_ALERT_LIFECYCLE.md for
the full schema/state-machine design and why each piece exists.

This module does not generate signals — it only records what `combiner.py`
already decided, and decides whether a re-poll is a cosmetic refresh, a
revision, or a cancellation of the previously-published instruction.
Nothing here changes signal-generation thresholds, weights, or SL/TP math.
"""
from __future__ import annotations
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
from sessions import SESSIONS, PRIMARY_SESSION, session_close_datetime  # noqa: E402

SCHEMA_VERSION = "alert_lifecycle_v1"

# --- decision points, flagged in IMPLEMENTATION_NOTE_ALERT_LIFECYCLE.md ---
DEFAULT_ENTRY_VALIDITY_MINUTES = 90       # mirrors sessions.DEFAULT_SIGNAL_VALIDITY_MINUTES, for continuity only
DEFAULT_MAX_HOLDING_HOURS = 30            # mirrors performance_scorer.MAX_LOOKAHEAD_HOURS, for continuity only
ENTRY_REFRESH_TOLERANCE_ATR_MULTIPLE = 0.05  # fraction of ATR a level may drift and still be a "cosmetic refresh"


def new_id() -> str:
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def guarded_entry_expiry(pair: str, published_at: datetime, validity_minutes: int = DEFAULT_ENTRY_VALIDITY_MINUTES) -> datetime:
    """Same idea as sessions.signal_window()'s valid_until, but guards the
    bug the read-only audit traced (CODEX_FOLLOWUP_FINDINGS.md item 1):
    session_close_datetime() always returns that CALENDAR DATE's close, even
    when `published_at` is already past it, which can make session_close <
    published_at. sessions.py itself is not touched — this is new, additive
    logic for the new alert schema only."""
    session_key = PRIMARY_SESSION.get(pair, "london")
    session_close = session_close_datetime(session_key, published_at.date())
    candidate = published_at + timedelta(minutes=validity_minutes)
    if session_close > published_at:
        return min(candidate, session_close)
    return candidate  # session already closed for today — fall back to the plain validity window, don't expire in the past


@dataclass
class AlertLevels:
    """The immutable, actionable content of one published version."""
    calculated_at_utc: str
    technical_inputs: dict
    pestle_inputs: dict | None
    pestle_used: bool
    quoted_midpoint: float
    bid_ask_observed: None  # always null today — see module docstring / IMPLEMENTATION_NOTE
    bid_ask_available: bool
    entry_price: float
    entry_tolerance: float
    entry_condition_lo: float
    entry_condition_hi: float
    stop: float
    target: float
    entry_expiry_utc: str
    max_holding_time_hours: float
    confidence: str
    combined_score: float
    reason: str


class AlertLifecycleStore:
    """Append-only JSONL store. Never edits or deletes a line once written —
    a `revised`/`cancelled` event is a NEW line, linked by lineage_id."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, event: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(event) + "\n")
        return event

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def feed_publish_status_by_event_id(self) -> dict[str, str]:
        """Latest feed_publish_result status per source lifecycle event_id
        (issued/revised/cancelled). Used both to decide what still needs
        (re)publishing and, in tests, to assert nothing was silently
        dropped between persistence and the user-facing feed."""
        status: dict[str, str] = {}
        for e in self.read_all():
            if e.get("event_type") == "feed_publish_result":
                status[e["source_event_id"]] = e["status"]
        return status

    def unpublished_events(self) -> list[dict]:
        """issued/revised/cancelled lifecycle events with no 'delivered'
        feed_publish_result — i.e. events that may exist only in this log,
        which the core acceptance rule says must not happen silently. A
        caller (live_scanner.py at the top of its next poll, or an
        operator running a recovery pass) can retry publishing these."""
        status = self.feed_publish_status_by_event_id()
        return [e for e in self.read_all()
                if e.get("event_type") in ("issued", "revised", "cancelled")
                and status.get(e["event_id"]) != "delivered"]

    def record_feed_publish_result(self, source_event: dict, status: str, error: str | None = None) -> dict:
        """Appends a feed_publish_result record linked to the lifecycle
        event it corresponds to. Never mutates the original event — keeps
        the append-only/immutable guarantee while making publish
        failures visible (status='failed') and recoverable (re-run
        unpublished_events() and retry) instead of silently assuming a
        written log line means a subscriber was notified."""
        return self.append({
            "event_id": new_id(), "event_type": "feed_publish_result",
            "source_event_id": source_event["event_id"],
            "alert_id": source_event["alert_id"], "lineage_id": source_event["lineage_id"],
            "pair": source_event["pair"],
            "status": status, "error": error,
            "recorded_at_utc": _utcnow().isoformat(),
        })

    def active_version_for_pair(self, pair: str, as_of: datetime | None = None) -> dict | None:
        """The most recent issued/revised event for `pair` whose lineage has
        not been cancelled and whose own entry window has not yet closed as
        of `as_of` (the moment of the NEW poll being classified — not the
        real wall clock, so this is fully deterministic for historical/test
        data). Used only to classify the NEXT candidate — the scorer itself
        replays full history independently (see alert_scorer.py) rather
        than trusting this convenience view."""
        as_of = as_of or _utcnow()
        events = [e for e in self.read_all()
                  if e.get("pair") == pair and e["event_type"] in ("issued", "revised", "cancelled")]
        if not events:
            return None
        by_lineage: dict[str, list[dict]] = {}
        for e in events:
            by_lineage.setdefault(e["lineage_id"], []).append(e)
        # most recently touched lineage
        latest_lineage = max(by_lineage.values(), key=lambda evs: max(ev["recorded_at_utc"] for ev in evs))
        latest_lineage.sort(key=lambda e: e["recorded_at_utc"])
        if latest_lineage[-1]["event_type"] == "cancelled":
            return None
        current = latest_lineage[-1]
        if current["event_type"] not in ("issued", "revised"):
            return None
        expiry = datetime.fromisoformat(current["entry_expiry_utc"])
        if expiry <= as_of:
            return None
        return current


def _within_tolerance(a: float, b: float, atr: float) -> bool:
    return abs(a - b) <= ENTRY_REFRESH_TOLERANCE_ATR_MULTIPLE * atr


# The stated policy for requirement 4: a revision or cancellation only ever
# closes a version's ENTRY window (no NEW entries under the old levels from
# that instant on). It never changes what happens to a subscriber who
# already entered under the version being superseded/cancelled — that
# assumed position keeps its OWN original stop, target and time exit,
# unaffected. alert_scorer.py's post-entry scan is not truncated at a
# later revision's timestamp for exactly this reason (see score_version).
EXISTING_POSITION_POLICY = "retain_original_stop_target_time_exit"


def _existing_position_snapshot(active: dict) -> dict:
    """Embedded in every revised/cancelled event so the published update
    can state, in the alert itself, exactly what an already-entered
    subscriber's original trade still looks like — not just link to it."""
    return {
        "alert_id": active["alert_id"],
        "policy": EXISTING_POSITION_POLICY,
        "original_stop": active["stop"],
        "original_target": active["target"],
        "original_max_holding_time_hours": active["max_holding_time_hours"],
        "original_entry_condition_lo": active["entry_condition_lo"],
        "original_entry_condition_hi": active["entry_condition_hi"],
    }


def classify_and_record(
    store: AlertLifecycleStore,
    *,
    scanner_version: str,
    pair: str,
    direction: str,        # "long" | "short" | "no_trade"
    confidence: str,
    combined_score: float,
    entry_price: float,
    atr_value: float,
    stop: float,
    target: float,
    technical_inputs: dict,
    pestle_inputs: dict | None,
    pestle_used: bool,
    reason: str,
    calculated_at: datetime,
    published_at: datetime | None = None,
    entry_tolerance: float | None = None,
    max_holding_time_hours: float = DEFAULT_MAX_HOLDING_HOURS,
) -> list[dict]:
    """The single entry point live_scanner.py calls. Returns EVERY event
    written this call, in order — normally zero (cosmetic refresh, nothing
    written) or one (issue/revise/cancel), but a direction reversal writes
    TWO (a cancellation of the old lineage, then a fresh issuance) and both
    must reach the caller so both reach the user-facing feed; returning
    only the last one was a real bug (the cancellation existed only in
    this log) fixed here per Codex's review. "Repeated polls / confidence
    flicker must not silently replace an alert" is satisfied by writing
    NOTHING for a cosmetic no-op, not by writing a heartbeat that could
    itself be mistaken for a new instruction."""
    published_at = published_at or calculated_at
    active = store.active_version_for_pair(pair, as_of=published_at)
    events: list[dict] = []

    if direction == "no_trade":
        if active is not None:
            events.append(store.append({
                "event_id": new_id(), "event_type": "cancelled",
                "alert_id": active["alert_id"], "lineage_id": active["lineage_id"],
                "cancelled_alert_id": active["alert_id"],
                "cancellation_reason": "scanner reverted to no_trade",
                "scanner_version": scanner_version, "pair": pair, "direction": direction,
                "recorded_at_utc": published_at.isoformat(),
                "existing_position": _existing_position_snapshot(active),
            }))
        return events  # no active alert to cancel, and no_trade never gets issued in the first place

    revision_source = active
    if active is not None and active["direction"] != direction:
        # reversal: a different trade thesis, not an update to this one —
        # cancel the old lineage, then fall through to issue a fresh one
        events.append(store.append({
            "event_id": new_id(), "event_type": "cancelled",
            "alert_id": active["alert_id"], "lineage_id": active["lineage_id"],
            "cancelled_alert_id": active["alert_id"],
            "cancellation_reason": f"direction reversed to {direction}",
            "scanner_version": scanner_version, "pair": pair, "direction": active["direction"],
            "recorded_at_utc": published_at.isoformat(),
            "existing_position": _existing_position_snapshot(active),
        }))
        active = None
        revision_source = None

    if active is not None:
        same_confidence = active["confidence"] == confidence
        levels_close = (
            _within_tolerance(active["entry_price"], entry_price, atr_value)
            and _within_tolerance(active["stop"], stop, atr_value)
            and _within_tolerance(active["target"], target, atr_value)
        )
        if same_confidence and levels_close:
            return events  # cosmetic refresh: repeated poll / confidence flicker on an unchanged setup — write nothing else

    tol = entry_tolerance if entry_tolerance is not None else max(0.1 * atr_value, 0.0)
    levels = AlertLevels(
        calculated_at_utc=calculated_at.isoformat(),
        technical_inputs=technical_inputs,
        pestle_inputs=pestle_inputs, pestle_used=pestle_used,
        quoted_midpoint=entry_price,
        bid_ask_observed=None, bid_ask_available=False,
        entry_price=entry_price, entry_tolerance=tol,
        entry_condition_lo=entry_price - tol, entry_condition_hi=entry_price + tol,
        stop=stop, target=target,
        entry_expiry_utc=guarded_entry_expiry(pair, published_at).isoformat(),
        max_holding_time_hours=max_holding_time_hours,
        confidence=confidence, combined_score=combined_score, reason=reason,
    )

    if active is not None:
        event = {
            "event_id": new_id(), "event_type": "revised",
            "alert_id": new_id(), "lineage_id": active["lineage_id"],
            "revises_alert_id": active["alert_id"],
            "revision_reason": ("confidence tier changed" if not same_confidence else "published levels moved beyond tolerance"),
            "scanner_version": scanner_version, "pair": pair, "direction": direction,
            "recorded_at_utc": published_at.isoformat(),
            "existing_position": _existing_position_snapshot(revision_source),
            **asdict(levels),
        }
    else:
        alert_id = new_id()
        event = {
            "event_id": new_id(), "event_type": "issued",
            "alert_id": alert_id, "lineage_id": alert_id,
            "revises_alert_id": None, "revision_reason": None,
            "scanner_version": scanner_version, "pair": pair, "direction": direction,
            "recorded_at_utc": published_at.isoformat(),
            "existing_position": None,
            **asdict(levels),
        }
    events.append(store.append(event))
    return events
