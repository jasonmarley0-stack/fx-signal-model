"""Droplet-native signal scanner: recomputes the combined signal for every
default pair against REAL live data — OANDA candles (technical) + Signal
Engine over localhost (PESTLE) — and logs every non-no_trade signal to the
same signals_log/ format eod_brief.py already scores.

This replaces monitor_signals.py's role for the droplet: monitor_signals.py
stays Mac-only (it depends on Dukascopy + a local Signal Engine + macOS
notifications, per the original design discussion). This script has no
Mac-specific dependencies, so it can run unattended on the droplet 24/7
instead of only firing when your Mac happens to be awake — closing that gap.

Polling on a fixed interval (not raw tick streaming) is a deliberate choice,
matching monitor_signals.py's existing cadence: it's what the model's
signals are actually generated on (30-min bars), it's far lighter on a
1GB droplet than holding an open streaming connection and buffering ticks
into bars by hand, and it reuses the exact scoring pipeline already tested
in the Mac monitor. Revisit only if sub-bar reaction time becomes a real
requirement.

Usage:
    python3 scanner_oanda.py [--pairs EURUSD,GBPUSD,...] [--once]

--once runs a single pass and exits (for manual testing); without it, the
script loops forever on a 30-minute cadence, aligned to :00/:30 past the
hour so it fires once each bar has actually closed. Meant to run under
systemd (see setup/signal-scanner.service) rather than launchd/cron.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from data.oanda import fetch_oanda_candles, fetch_current_price  # noqa: E402
from dashboard_data import DEFAULT_PAIRS  # noqa: E402
from strategies.composite import technical_score  # noqa: E402
from pestle.pestle_scorer import pair_pestle_score  # noqa: E402
from pestle.signal_engine_client import SignalEngineClient  # noqa: E402
from combiner import combine_signal  # noqa: E402

SIGNALS_LOG_DIR = Path(__file__).parent / "signals_log"
LIVE_SCAN_PATH = Path(__file__).parent / "live_scan.json"
POLL_SECONDS = 30 * 60


def log_signal(pair: str, sig) -> None:
    if sig.direction == "no_trade":
        return
    SIGNALS_LOG_DIR.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = SIGNALS_LOG_DIR / f"{today}.jsonl"
    entry = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "pair": pair,
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


def write_live_scan(now: datetime, rows: list[dict]) -> None:
    """Snapshot of the most recent scan for the dashboard to read — the
    dashboard never talks to OANDA/Signal Engine directly, it just reads
    this file, so a slow/dead dashboard page can never block the scanner."""
    payload = {"generated_at": now.isoformat(), "rows": rows}
    tmp_path = LIVE_SCAN_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(LIVE_SCAN_PATH)  # atomic — dashboard never reads a half-written file


def run_once(pairs: list[str], client: SignalEngineClient) -> None:
    now = datetime.now(timezone.utc)
    fired = 0
    rows = []
    for pair in pairs:
        try:
            df = fetch_oanda_candles(pair, count=300, granularity="M30")
            if len(df) < 60:
                print(f"  {pair}: not enough OANDA history yet ({len(df)} bars) — skipping")
                rows.append({"pair": pair, "error": f"not enough OANDA history ({len(df)} bars)"})
                continue
            tech = technical_score(df)
            latest_tech = tech.iloc[-1]
            pestle = pair_pestle_score(pair, client=client)
            entry = fetch_current_price(pair)
            latest_bar_time = df.index[-1]

            sig = combine_signal(
                pair=pair, entry=entry, atr_value=latest_tech["atr"],
                tech_score=latest_tech["tech_score"], pestle_score=pestle["pestle_score"],
                generated_at=latest_bar_time.to_pydatetime(),
            )
            log_signal(pair, sig)
            if sig.direction != "no_trade":
                fired += 1
                print(f"  {pair}: {sig.direction.upper()} ({sig.confidence}) @ {entry:.5f} — {sig.reason}")
            else:
                print(f"  {pair}: no_trade ({sig.reason})")

            window = None
            if sig.window:
                window = {
                    "session": sig.window.get("session"),
                    "generated_at_gmt_str": sig.window.get("generated_at_gmt_str"),
                    "valid_until_gmt_str": sig.window.get("valid_until_gmt_str"),
                }
            rows.append({
                "pair": pair, "entry": entry,
                "tech": {"orb": latest_tech["orb"], "trend": latest_tech["trend"],
                         "pattern": latest_tech["pattern"], "composite": latest_tech["tech_score"]},
                "pestle_score": pestle["pestle_score"],
                "direction": sig.direction, "confidence": sig.confidence,
                "combined_score": sig.combined_score,
                "stop_loss_range": list(sig.stop_loss_range),
                "take_profit_range": list(sig.take_profit_range),
                "reason": sig.reason, "window": window,
            })
        except Exception as e:  # noqa: BLE001 — one pair's failure shouldn't kill the run
            print(f"  {pair}: ERROR — {e}")
            rows.append({"pair": pair, "error": str(e)})
    write_live_scan(now, rows)
    print(f"[{now.isoformat()}] Scan complete — {fired} signal(s) fired across {len(pairs)} pairs.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=str, default=",".join(DEFAULT_PAIRS))
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit (for manual testing)")
    args = parser.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",") if p.strip()]

    client = SignalEngineClient()
    if client.mock:
        print("Signal Engine not reachable (mock mode) — is the signal-engine systemd service running "
              "and SIGNAL_ENGINE_BASE_URL set to http://127.0.0.1:8000? Continuing in mock mode anyway.")

    if args.once:
        run_once(pairs, client)
        return

    print(f"Scanner starting — {len(pairs)} pairs, polling every 30 minutes. Ctrl+C or systemctl stop to quit.")
    while True:
        run_once(pairs, client)
        # Align next wake to the next :00/:30 past the hour, not just "30 min from now" —
        # keeps it in sync with when OANDA actually closes each M30 bar.
        now = time.time()
        next_wake = (now // POLL_SECONDS + 1) * POLL_SECONDS
        time.sleep(max(5, next_wake - now))


if __name__ == "__main__":
    main()
