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

Usage:
    python3 live_scanner.py

Meant to run under systemd on a recurring timer (see
setup/live-scanner.service + .timer), not interactively.
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "src"))

from data.oanda import fetch_oanda_candles, fetch_current_price  # noqa: E402
from strategies.composite import technical_score  # noqa: E402
from combiner import combine_signal  # noqa: E402

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"]
GRANULARITY = "H4"
HISTORY_BARS = 300  # ~50 days — plenty of warmup for EMA50/ATR14
SPARKLINE_BARS = 48  # ~8 days of H4 bars for the Live tab trendline
ALPHA = 1.0  # tech-only — see module docstring

STATE_PATH = Path(__file__).parent / ".live_scanner_state.json"
SIGNALS_LOG_DIR = Path(__file__).parent / "signals_log"
ALERTS_PATH = Path(__file__).parent / "alerts.json"
LIVE_SCAN_PATH = Path(__file__).parent / "live_scan.json"
MAX_ALERTS = 50


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


def push_alert(pair: str, sig, tech: dict, pestle: dict) -> None:
    alerts = []
    if ALERTS_PATH.exists():
        try:
            alerts = json.loads(ALERTS_PATH.read_text()).get("alerts", [])
        except json.JSONDecodeError:
            alerts = []
    label = f"{sig.direction.upper()} ({sig.confidence}) @ {sig.entry:.5f}" if sig.direction != "no_trade" else "back to no_trade"
    entry = {
        "id": f"{int(time.time() * 1000)}-{pair}",
        "time": datetime.now(timezone.utc).isoformat(),
        "pair": pair, "direction": sig.direction, "confidence": sig.confidence,
        "combined_score": sig.combined_score, "entry": sig.entry,
        "message": f"{pair}: {label}", "reason": sig.reason,
        "tech": tech, "pestle": pestle,
        "stop_loss_range": list(sig.stop_loss_range),
        "take_profit_range": list(sig.take_profit_range),
        "window": None,
    }
    alerts.append(entry)
    alerts = alerts[-MAX_ALERTS:]
    tmp = ALERTS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"alerts": alerts}, indent=2))
    tmp.replace(ALERTS_PATH)


def write_live_scan(now: datetime, rows: list[dict]) -> None:
    payload = {"generated_at": now.isoformat(), "rows": rows}
    tmp_path = LIVE_SCAN_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(LIVE_SCAN_PATH)


def main() -> None:
    state = load_state()
    rows = []
    now = datetime.now(timezone.utc)

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

            prev = state.get(pair)
            state[pair] = sig.direction
            if prev is not None and sig.direction != prev:
                log_signal(pair, sig)
                push_alert(pair, sig, tech_dict, pestle_dict)

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
