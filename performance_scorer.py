"""Droplet-native signal-outcome + directional-accuracy scorer.

Ports eod_brief.py's score_signal() logic off Dukascopy (Mac-only) onto
OANDA (already the droplet's live data source), runs on a recurring
schedule instead of once/day by hand, and aggregates across every day
instead of producing one standalone HTML file per day. Feeds the
Performance tab per SIGNAL_IQ_GAP_ANALYSIS.md item 1c and the decisions in
SIGNAL_DEFINITION_AND_ACCURACY.md.

Every fired (non-no_trade) signal in signals_log/ gets scored two ways,
independently, both against real OANDA price action:

  - Trade-outcome: did price hit the signal's own stated SL or TP first
    (or neither within MAX_LOOKAHEAD_HOURS, "unresolved") -> R-multiple.
    Same method eod_brief.py already used.
  - Directional accuracy: did price simply move the called direction by
    DIRECTIONAL_HORIZON_HOURS after firing, independent of the specific
    SL/TP levels. This is the simpler, more marketable metric, and the
    dataset a future probability calibration would be checked against —
    see SIGNAL_DEFINITION_AND_ACCURACY.md for why both are tracked rather
    than just one.

Every run rescores every signal instance from scratch (cheap — signal
volume is low, OANDA calls are the only cost) rather than trying to track
which ones are "done", so a signal's outcome naturally firms up from
"unresolved"/"pending" to a real result as more real price data accumulates
run over run.

Results are persisted to performance.json — a flat list of scored signal
instances plus pre-computed 7D/30D/All aggregates — rather than rendered as
a page, so a future dashboard_server.py /api/performance endpoint can just
read it directly.

Usage:
    python3 performance_scorer.py

Meant to run under systemd on a recurring timer (see
setup/performance-scorer.service + .timer), not interactively.
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from data.oanda import fetch_oanda_candles  # noqa: E402

SIGNALS_LOG_DIR = Path(__file__).parent / "signals_log"
PERFORMANCE_PATH = Path(__file__).parent / "performance.json"

MAX_LOOKAHEAD_HOURS = 30  # trade-outcome: give up and call it "unresolved" past this, matches eod_brief.py's convention
DIRECTIONAL_HORIZON_HOURS = 24  # directional accuracy: a fixed rolling check-in point, not a UTC-midnight "end of day" —
# meaningful regardless of what time of day a signal actually fired (see SIGNAL_DEFINITION_AND_ACCURACY.md)


def load_all_signals() -> list[dict]:
    """Reads every signals_log/*.jsonl entry across all history (no_trade
    signals are never logged in the first place — see log_signal() in
    streaming_scanner.py/scanner_oanda.py — so nothing needs filtering here).

    Dedupes by (pair, the bar it was generated on, direction, confidence).
    The old 30-min poller (scanner_oanda.py) logged the same still-holding
    signal every pass until it changed, so several lines can describe the
    same instance; the streaming scanner only logs on a genuine transition,
    so this key naturally collapses to one row per instance there too.
    Last write wins — entry/SL/TP are stable per instance regardless."""
    if not SIGNALS_LOG_DIR.exists():
        return []
    raw = []
    for log_path in sorted(SIGNALS_LOG_DIR.glob("*.jsonl")):
        for line in log_path.read_text().splitlines():
            if line.strip():
                raw.append(json.loads(line))

    seen: dict[tuple, dict] = {}
    for e in raw:
        key = (e["pair"], e.get("window", {}).get("generated_at_utc") or e["logged_at"],
               e["direction"], e["confidence"])
        seen[key] = e
    return list(seen.values())


def score_trade_outcome(entry: dict) -> dict:
    """Did price hit the signal's own stated stop or target first. Ported
    from eod_brief.py's score_signal() — same stop/target selection logic
    (combiner.py's SL/TP ranges are asymmetric; the nearer/more
    conservative bound is the actual stop, the far bound is the full
    target), swapped from Dukascopy onto fetch_oanda_candles()."""
    pair = entry["pair"]
    direction = 1 if entry["direction"] == "long" else -1
    sl_lo, sl_hi = entry["stop_loss_range"]
    tp_lo, tp_hi = entry["take_profit_range"]
    stop = sl_hi if direction == 1 else sl_lo
    target = tp_hi if direction == 1 else tp_lo

    start = datetime.fromisoformat(entry["logged_at"].replace("Z", "+00:00"))
    end = min(start + timedelta(hours=MAX_LOOKAHEAD_HOURS), datetime.now(timezone.utc))

    result = {"stop": stop, "target": target, "outcome": "unresolved", "exit_price": None, "r_multiple": None}
    if end <= start:
        return result

    df = fetch_oanda_candles(pair, granularity="M30", from_time=start, to_time=end, count=None)
    if df.empty:
        result["outcome"] = "no_data"
        return result

    entry_price = entry["entry"]
    risk = abs(entry_price - stop)
    if risk == 0:
        result["outcome"] = "no_data"
        return result

    for _, bar in df.iterrows():
        hit_stop = (bar["low"] <= stop) if direction == 1 else (bar["high"] >= stop)
        hit_target = (bar["high"] >= target) if direction == 1 else (bar["low"] <= target)
        if hit_stop:  # conservative — a bar that could have hit either is scored as the stop
            result["outcome"], result["exit_price"] = "stop", stop
            break
        if hit_target:
            result["outcome"], result["exit_price"] = "target", target
            break

    if result["outcome"] in ("stop", "target"):
        result["r_multiple"] = direction * (result["exit_price"] - entry_price) / risk
    return result


def score_directional(entry: dict) -> dict:
    """Did price simply move the called direction by DIRECTIONAL_HORIZON_HOURS
    after firing — independent of the specific SL/TP levels."""
    pair = entry["pair"]
    fired_at = datetime.fromisoformat(entry["logged_at"].replace("Z", "+00:00"))
    horizon = fired_at + timedelta(hours=DIRECTIONAL_HORIZON_HOURS)
    result = {"directional_horizon_hours": DIRECTIONAL_HORIZON_HOURS,
              "directional_outcome": "pending", "directional_price": None}

    if datetime.now(timezone.utc) < horizon:
        return result

    df = fetch_oanda_candles(pair, granularity="M30",
                              from_time=horizon - timedelta(minutes=30),
                              to_time=horizon + timedelta(minutes=30), count=None)
    if df.empty:
        result["directional_outcome"] = "no_data"
        return result

    price_at_horizon = float(df["close"].iloc[-1])
    moved_up = price_at_horizon > entry["entry"]
    called_up = entry["direction"] == "long"
    result["directional_price"] = price_at_horizon
    result["directional_outcome"] = "correct" if moved_up == called_up else "incorrect"
    return result


def aggregate(scored: list[dict], since: datetime | None = None) -> dict:
    if since is not None:
        scored = [s for s in scored
                  if datetime.fromisoformat(s["logged_at"].replace("Z", "+00:00")) >= since]

    resolved = [s for s in scored if s["outcome"] in ("stop", "target")]
    wins = [s for s in resolved if s["outcome"] == "target"]
    directional_done = [s for s in scored if s["directional_outcome"] in ("correct", "incorrect")]
    directional_correct = [s for s in directional_done if s["directional_outcome"] == "correct"]

    by_pair = {}
    for pair in sorted({s["pair"] for s in scored}):
        pair_all = [s for s in scored if s["pair"] == pair]
        pair_resolved = [s for s in resolved if s["pair"] == pair]
        pair_wins = [s for s in pair_resolved if s["outcome"] == "target"]
        pair_directional_done = [s for s in pair_all if s["directional_outcome"] in ("correct", "incorrect")]
        pair_directional_correct = [s for s in pair_directional_done if s["directional_outcome"] == "correct"]
        r_values = [s["r_multiple"] for s in pair_resolved]
        by_pair[pair] = {
            "signals": len(pair_all),
            "win_rate": (len(pair_wins) / len(pair_resolved)) if pair_resolved else None,
            "avg_r": (sum(r_values) / len(r_values)) if r_values else None,
            "best_r": max(r_values) if r_values else None,
            "worst_r": min(r_values) if r_values else None,
            "directional_accuracy": (len(pair_directional_correct) / len(pair_directional_done)) if pair_directional_done else None,
        }
    ranked = [(p, s["win_rate"]) for p, s in by_pair.items() if s["win_rate"] is not None]
    best_pair = max(ranked, key=lambda kv: kv[1])[0] if ranked else None

    cumulative, running = [], 0.0
    for s in sorted(resolved, key=lambda s: s["logged_at"]):
        running += s["r_multiple"]
        cumulative.append({"time": s["logged_at"], "pair": s["pair"], "cumulative_r": round(running, 3)})

    r_values_all = [s["r_multiple"] for s in resolved]
    return {
        "total_signals": len(scored),
        "resolved_signals": len(resolved),
        "win_rate": (len(wins) / len(resolved)) if resolved else None,
        "avg_r": (sum(r_values_all) / len(r_values_all)) if r_values_all else None,
        "directional_accuracy": (len(directional_correct) / len(directional_done)) if directional_done else None,
        "directional_sample_size": len(directional_done),
        "best_pair": best_pair,
        "by_pair": by_pair,
        "cumulative_r_series": cumulative,
    }


def main():
    entries = load_all_signals()
    print(f"{len(entries)} unique signal instance(s) across all logs.")

    scored = []
    for e in entries:
        try:
            outcome = score_trade_outcome(e)
        except Exception as ex:  # noqa: BLE001 — one bad instance shouldn't kill the whole run
            print(f"  {e['pair']} {e['direction']} @ {e['entry']:.5f} — trade-outcome scoring ERROR: {ex}")
            outcome = {"stop": None, "target": None, "outcome": "no_data", "exit_price": None, "r_multiple": None}
        try:
            directional = score_directional(e)
        except Exception as ex:  # noqa: BLE001
            print(f"  {e['pair']} {e['direction']} @ {e['entry']:.5f} — directional scoring ERROR: {ex}")
            directional = {"directional_horizon_hours": DIRECTIONAL_HORIZON_HOURS,
                            "directional_outcome": "no_data", "directional_price": None}

        merged = dict(e)
        merged.update(outcome)
        merged.update(directional)
        scored.append(merged)

        r_str = f" ({outcome['r_multiple']:+.2f}R)" if outcome.get("r_multiple") is not None else ""
        print(f"  {e['pair']} {e['direction']} @ {e['entry']:.5f} -> {outcome['outcome']}{r_str} "
              f"| directional: {directional['directional_outcome']}")

    now = datetime.now(timezone.utc)
    payload = {
        "updated_at": now.isoformat(),
        "signals": scored,
        "aggregates": {
            "7d": aggregate(scored, since=now - timedelta(days=7)),
            "30d": aggregate(scored, since=now - timedelta(days=30)),
            "all": aggregate(scored, since=None),
        },
    }
    tmp_path = PERFORMANCE_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(PERFORMANCE_PATH)  # atomic — same convention as live_scan.json/alerts.json

    all_agg = payload["aggregates"]["all"]
    print(f"\nWrote {PERFORMANCE_PATH} — {len(scored)} signal(s), "
          f"{all_agg['resolved_signals']} resolved, "
          f"{all_agg['directional_sample_size']} directional-scored.")


if __name__ == "__main__":
    main()
