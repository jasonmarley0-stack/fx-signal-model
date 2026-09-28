"""Primary live scanner — replaces streaming_scanner.py's tick-driven M30
setup with the validated configuration (see NEXT_STEPS.md, "transaction
cost modeling" + "cost-aware grid search", 2026-09-13/23):

  - H4 bars, majors only (EURUSD/GBPUSD/USDJPY/USDCHF/AUDUSD/USDCAD/NZDUSD)
  - original/baseline ORB-trend-pattern weights (strategies.composite's
    defaults — the reweighted variant did NOT hold up on majors+H4;
    baseline did: n=75, cost-adjusted win_rate=0.56, avg_r=+0.026)
  - tech-only (alpha=1.0, no PESTLE) — that's what was actually backtested;
    PESTLE has never been tested at this timeframe/pair-scope combination
    (its own history is still too short — see NEXT_STEPS.md), so it's left
    out here rather than assumed to help. Revisit once there's enough real
    PESTLE history to test properly, not before.
  - the spread-floored stop from combiner.py (2026-09-13) — every stop is
    now sized against real trading cost, not just ATR.

Deliberately a periodic REST poll, not a persistent tick stream — the
tick-driven architecture streaming_scanner.py used was found to be the
dominant source of noise (scoring off a still-forming candle every ~30s
produced spurious direction flips; see NEXT_STEPS.md "closed-bar vs
live-bar"). fetch_oanda_candles() already excludes the in-progress candle,
so a periodic poll sidesteps that problem entirely by construction, the
same way shadow_scanner.py did.

Writes to the SAME production paths streaming_scanner.py used
(signals_log/, alerts.json, live_scan.json, performance.json via the
existing performance_scorer.py) — the dashboard and performance scoring
don't need to change, only what feeds them does.

Also writes, on every poll (not only on a direction change), to
alert_lifecycle_log.jsonl via alert_lifecycle.py — an immutable,
revision/cancellation-aware record of exactly what was published, scored
separately by alert_scorer.py. signals_log/alerts.json are now driven by
that SAME classification (via alert_feed_publisher.record_and_publish),
not a second, independently-gated check — a same-direction revision (a
real, meaningful re-level, not noise) reaches the feed exactly like a
direction change always did; a cosmetic refresh reaches neither. See
IMPLEMENTATION_NOTE_ALERT_LIFECYCLE.md and CODEX_ALERT_LIFECYCLE_CORRECTIONS.md.

Usage:
    python3 live_scanner.py

Meant to run under systemd on a recurring timer (see
setup/live-scanner.service + .timer), not interactively.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "src"))

from data.oanda import fetch_oanda_candles, fetch_current_price  # noqa: E402
from strategies.composite import technical_score  # noqa: E402
from combiner import combine_signal  # noqa: E402
from alert_lifecycle import AlertLifecycleStore  # noqa: E402
from alert_feed_publisher import record_and_publish, retry_unpublished  # noqa: E402

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
GRANULARITY = "H4"
HISTORY_BARS = 300  # ~50 days — plenty of warmup for EMA50/ATR14
SPARKLINE_BARS = 48  # ~8 days of H4 bars for the Live tab trendline
ALPHA = 1.0  # tech-only — see module docstring

# Bump this string whenever the live configuration meaningfully changes
# (pairs, granularity, weights, alpha) — alert_performance_view.py keys
# every aggregate off it so different configurations are never blended.
SCANNER_VERSION = "H4_majors_baseline_v1"

STATE_PATH = Path(__file__).parent / ".live_scanner_state.json"
SIGNALS_LOG_DIR = Path(__file__).parent / "signals_log"
ALERTS_PATH = Path(__file__).parent / "alerts.json"
LIVE_SCAN_PATH = Path(__file__).parent / "live_scan.json"
ALERT_LIFECYCLE_PATH = Path(__file__).parent / "alert_lifecycle_log.jsonl"

_alert_store = AlertLifecycleStore(ALERT_LIFECYCLE_PATH)


def record_alert_lifecycle_and_publish(pair: str, sig, tech_dict: dict, pestle_dict: dict, atr_value: float, now: datetime) -> list[dict]:
    """The single call that both persists (alert_lifecycle_log.jsonl) and
    publishes to the user-facing feed (alerts.json) for this poll's
    classification — issued / revised / cancelled (possibly both a
    cancellation and a fresh issuance, on a direction reversal), or nothing
    for a cosmetic refresh. Callers wrap this in try/except so a bug here
    can never affect live_scan.json (the table view), which is written
    unconditionally regardless of this call's outcome. See
    IMPLEMENTATION_NOTE_ALERT_LIFECYCLE.md / CODEX_ALERT_LIFECYCLE_CORRECTIONS.md."""
    if sig.direction == "long":
        stop, target = sig.stop_loss_range[1], sig.take_profit_range[1]
    elif sig.direction == "short":
        stop, target = sig.stop_loss_range[0], sig.take_profit_range[0]
    else:
        stop, target = None, None
    return record_and_publish(
        _alert_store, ALERTS_PATH, tech_dict, pestle_dict,
        scanner_version=SCANNER_VERSION, pair=pair, direction=sig.direction,
        confidence=sig.confidence, combined_score=sig.combined_score,
        entry_price=sig.entry, atr_value=atr_value, stop=stop, target=target,
        technical_inputs=tech_dict, pestle_inputs=None, pestle_used=False,
        reason=sig.reason, calculated_at=now, published_at=now,
    )


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def empty_pestle(pair: str) -> dict:
    """Zeroed, schema-compatible pestle dict — PESTLE isn't used by this
    scanner (see module docstring), but the dashboard's alert cards expect
    this shape (base/quote/evidence), so this renders honestly as "not
    used" rather than breaking the card or silently faking a real score."""
    return {
        "pair": pair, "pestle_score": 0.0,
        "base": {"currency": pair[:3], "score": 0.0, "categories": {}, "evidence": []},
        "quote": {"currency": pair[3:], "score": 0.0, "categories": {}, "evidence": []},
    }


def log_signal(pair: str, sig) -> None:
    if sig.direction == "no_trade":
        return
    SIGNALS_LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = SIGNALS_LOG_DIR / f"{today}.jsonl"
    entry = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "pair": pair, "direction": sig.direction, "confidence": sig.confidence,
        "combined_score": sig.combined_score, "tech_score": sig.tech_score,
        "pestle_score": sig.pestle_score, "entry": sig.entry,
        "stop_loss_range": list(sig.stop_loss_range),
        "take_profit_range": list(sig.take_profit_range),
        "reason": sig.reason, "window": None,
    }
    with log_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print(f"[fire] {pair}: {sig.direction.upper()} ({sig.confidence}) @ {sig.entry:.5f} — {sig.reason}")


def write_live_scan(now: datetime, rows: list[dict]) -> None:
    payload = {"generated_at": now.isoformat(), "rows": rows}
    tmp_path = LIVE_SCAN_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(LIVE_SCAN_PATH)


def main() -> None:
    state = load_state()
    rows = []
    now = datetime.now(timezone.utc)

    try:
        retried = retry_unpublished(_alert_store, ALERTS_PATH)
        if retried:
            print(f"[recovery] republished {len(retried)} lifecycle event(s) that hadn't reached the feed")
    except Exception as retry_ex:
        print(f"  alert-feed recovery pass error (non-fatal) — {retry_ex}")

    for pair in PAIRS:
        try:
            df = fetch_oanda_candles(pair, granularity=GRANULARITY, count=HISTORY_BARS)
            if len(df) < 60:
                rows.append({"pair": pair, "error": f"not enough history yet ({len(df)} bars)"})
                continue
            scored = technical_score(df)  # baseline weights (default) — see module docstring
            latest = scored.iloc[-1]
            entry_price = fetch_current_price(pair)

            sig = combine_signal(pair=pair, entry=entry_price, atr_value=float(latest["atr"]),
                                  tech_score=float(latest["tech_score"]), pestle_score=0.0, alpha=ALPHA)

            tech_dict = {"orb": float(latest["orb"]), "trend": float(latest["trend"]),
                         "pattern": float(latest["pattern"]), "composite": float(latest["tech_score"])}
            pestle_dict = empty_pestle(pair)

            # state[pair] is kept for observability only — gating now comes
            # entirely from alert_lifecycle_log.jsonl's own history (via
            # classify_and_record's active_version_for_pair), which is why
            # a genuine FIRST issuance for a pair now fires correctly
            # instead of being silently suppressed the way the old
            # prev-is-None guard used to suppress it.
            state[pair] = sig.direction
            try:
                lifecycle_events = record_alert_lifecycle_and_publish(
                    pair, sig, tech_dict, pestle_dict, float(latest["atr"]), now)
            except Exception as lifecycle_ex:
                lifecycle_events = []
                print(f"  {pair}: alert-lifecycle error (non-fatal, live_scan.json table still updates) — {lifecycle_ex}")
            # legacy signals_log — driven by the SAME classification as the
            # feed now, not a second independent direction-change check
            for event in lifecycle_events:
                if event["event_type"] in ("issued", "revised"):
                    log_signal(pair, sig)

            sparkline = [round(float(v), 6) for v in df["close"].tail(SPARKLINE_BARS).tolist()]
            rows.append({
                "pair": pair, "entry": entry_price, "price_arrow": "flat",
                "sparkline": sparkline, "tech": tech_dict, "pestle_score": 0.0,
                "direction": sig.direction, "confidence": sig.confidence,
                "combined_score": sig.combined_score,
                "stop_loss_range": list(sig.stop_loss_range),
                "take_profit_range": list(sig.take_profit_range),
                "reason": sig.reason, "window": None,
            })
        except Exception as ex:
            rows.append({"pair": pair, "error": str(ex)})
            print(f"  {pair}: error — {ex}")

    write_live_scan(now, rows)
    save_state(state)


if __name__ == "__main__":
    main()
