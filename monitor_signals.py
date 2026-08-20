"""Intraday monitor: recomputes the combined signal for every default pair
against REAL data (local Signal Engine + Dukascopy), and fires a native
macOS notification only when something crosses the "worth a look" bar —
this is not the morning report, it's the "hey Jase, look at this" alert.

Runs entirely on your Mac (this is why — Signal Engine and your Dukascopy
access only exist locally, see the design discussion in chat). Meant to be
scheduled via launchd every 30 min during trading sessions; see
setup/com.fx-signal-model.monitor.plist and MONITOR_SETUP.md.

Trigger conditions (either is enough):
  - combined signal confidence == "high" (|combined_score| >= 0.6)
  - a PESTLE signal newer than --alert-lookback-hours landed for either
    currency in a pair, regardless of current combined confidence — you
    said you want to know when "something is happening", not just when
    the model already reached a strong verdict.

State is tracked in .monitor_state.json (next to this script) so the same
setup doesn't re-alert every 30 minutes — only genuinely new triggers fire.

Usage:
    python3 monitor_signals.py [--pairs EURUSD,GBPUSD,...] [--days 3]
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from backtest import load_real_ohlc  # noqa: E402
from dashboard_data import DEFAULT_PAIRS, build_dashboard_payload  # noqa: E402
from pestle.signal_engine_client import SignalEngineClient  # noqa: E402

STATE_PATH = Path(__file__).parent / ".monitor_state.json"
ALERT_CONFIDENCE = "high"
ALERT_LOOKBACK_HOURS = 3  # "something just happened" window, separate from the 72h scoring lookback
SIGNALS_LOG_DIR = Path(__file__).parent / "signals_log"


def log_signal(row: dict) -> None:
    """Appends every non-no_trade signal to a daily JSONL file, for the
    end-of-day brief to score against real price action later. Not just
    alerted signals — every one the model actually fired, per the scoping
    decision in chat."""
    sig = row["signal"]
    if sig.direction == "no_trade":
        return
    SIGNALS_LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = SIGNALS_LOG_DIR / f"{today}.jsonl"
    entry = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "pair": row["pair"],
        "direction": sig.direction,
        "confidence": sig.confidence,
        "combined_score": sig.combined_score,
        "tech_score": sig.tech_score,
        "pestle_score": sig.pestle_score,
        "entry": sig.entry,
        "stop_loss_range": list(sig.stop_loss_range),
        "take_profit_range": list(sig.take_profit_range),
        "reason": sig.reason,
        "window": None,
    }
    if sig.window:
        w = dict(sig.window)
        w["generated_at_utc"] = w["generated_at_utc"].isoformat()
        w["valid_until_utc"] = w["valid_until_utc"].isoformat()
        entry["window"] = w
    with log_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def notify(title: str, message: str) -> None:
    script = f'display notification {json.dumps(message)} with title {json.dumps(title)} sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=str, default=",".join(DEFAULT_PAIRS))
    parser.add_argument("--days", type=int, default=3, help="How much OHLC history to pull per check (keep small — this runs every 30 min)")
    args = parser.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]

    client = SignalEngineClient()
    if client.mock:
        print("Signal Engine not reachable (mock mode) — is `uvicorn app.main:app --reload` running? Skipping this check.")
        sys.exit(0)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    state = load_state()
    alerts = []

    pair_dfs = {}
    for pair in pairs:
        df = load_real_ohlc(pair, start, end, freq="30min")
        if not df.empty and len(df) >= 60:
            pair_dfs[pair] = df

    if not pair_dfs:
        print("No usable OHLC data this run — skipping (network issue to Dukascopy?).")
        sys.exit(0)

    payload = build_dashboard_payload(pair_dfs, pairs=list(pair_dfs.keys()))

    for row in payload["rows"]:
        pair = row["pair"]
        sig = row["signal"]
        pestle = row["pestle"]
        prev = state.get(pair, {})

        log_signal(row)

        reasons = []
        if sig.confidence == ALERT_CONFIDENCE and sig.direction != "no_trade":
            if prev.get("last_alerted_direction") != sig.direction or prev.get("last_alerted_confidence") != sig.confidence:
                reasons.append(f"High-confidence {sig.direction.upper()} signal (combined {sig.combined_score:+.2f})")

        for side in ("base", "quote"):
            currency = pestle[side]["currency"]
            recent_signals = client.fetch_signals(currency, lookback_hours=ALERT_LOOKBACK_HOURS)
            # SCHEDULED_HIGH_IMPACT_EVENT is a forward-looking calendar flag, not
            # real market-moving evidence (see calendar_events.py) — it's
            # deliberately excluded from PESTLE scoring, and must be excluded
            # here too, or seeding a calendar event falsely triggers an alert.
            new_codes = {s.signal_type_code for s in recent_signals if s.signal_type_code != "SCHEDULED_HIGH_IMPACT_EVENT"}
            seen_codes = set(prev.get("seen_pestle_codes", []))
            fresh = new_codes - seen_codes
            if fresh:
                reasons.append(f"Fresh {currency} PESTLE evidence: {', '.join(sorted(fresh))}")
            prev.setdefault("seen_pestle_codes", [])
            prev["seen_pestle_codes"] = sorted(seen_codes | new_codes)

        if reasons:
            alerts.append((pair, sig, reasons))

        state[pair] = {
            "last_alerted_direction": sig.direction,
            "last_alerted_confidence": sig.confidence,
            "seen_pestle_codes": prev["seen_pestle_codes"],
        }

    save_state(state)

    if not alerts:
        print(f"[{end.isoformat()}] Checked {len(pair_dfs)} pairs — nothing new to flag.")
        return

    for pair, sig, reasons in alerts:
        title = f"Hey Jase — {pair} setup"
        window_str = ""
        if sig.window:
            window_str = f" Window: {sig.window['generated_at_gmt_str']}–{sig.window['valid_until_gmt_str']}."
        message = (
            f"{sig.direction.upper()} ({sig.confidence} confidence). "
            + " / ".join(reasons)
            + f".{window_str} SL {min(sig.stop_loss_range):.5f}-{max(sig.stop_loss_range):.5f}, "
              f"TP {min(sig.take_profit_range):.5f}-{max(sig.take_profit_range):.5f}."
        )
        print(f"ALERT: {title} — {message}")
        notify(title, message)

    # Refresh the local dashboard file too, so opening it right after an alert
    # shows current detail. This does NOT update the persisted Cowork artifact —
    # that update has to come from a Claude session with the device bridge
    # connected; see MONITOR_SETUP.md for how to sync it after an alert.
    sys.path.insert(0, str(Path(__file__).parent / "dashboard"))
    from generate_dashboard import render_dashboard  # noqa: E402

    def serialize(row):
        sig = row["signal"]
        row = dict(row)
        window = None
        if sig.window:
            window = dict(sig.window)
            window["generated_at_utc"] = window["generated_at_utc"].isoformat()
            window["valid_until_utc"] = window["valid_until_utc"].isoformat()
        row["signal"] = {
            "direction": sig.direction, "confidence": sig.confidence,
            "combined_score": sig.combined_score, "entry": sig.entry,
            "stop_loss_range": list(sig.stop_loss_range),
            "take_profit_range": list(sig.take_profit_range), "reason": sig.reason,
            "window": window,
        }
        return row

    payload["rows"] = [serialize(r) for r in payload["rows"]]
    out_dir = Path(__file__).parent
    (out_dir / "live_dashboard_payload.json").write_text(json.dumps(payload, indent=2, default=str))
    (out_dir / "live_morning_dashboard.html").write_text(render_dashboard(payload))
    print("Refreshed live_morning_dashboard.html with the alert-triggering data.")


if __name__ == "__main__":
    main()
