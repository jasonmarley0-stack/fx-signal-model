"""End-of-day brief: takes every signal monitor_signals.py logged today,
checks real price action against each one's actual SL/TP levels, and
reports how the model actually performed — not a backtest, a scorecard for
today specifically.

Runs locally (same reason as everything else — needs Dukascopy access).
Scheduled via com.fx-signal-model.eod-brief.plist at ~22:00 local, after NY
close, per the scoping decision in chat.

Usage:
    python3 eod_brief.py [--date 2026-08-19]  # defaults to today (UTC)
"""
from __future__ import annotations
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent / "dashboard"))

from backtest import load_real_ohlc  # noqa: E402

SIGNALS_LOG_DIR = Path(__file__).parent / "signals_log"
MAX_LOOKAHEAD_HOURS = 30  # if neither SL nor TP is hit within this, mark "unresolved" rather than wait forever


def load_day_signals(date_str: str) -> list[dict]:
    log_path = SIGNALS_LOG_DIR / f"{date_str}.jsonl"
    if not log_path.exists():
        return []
    entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    # Dedupe: the monitor logs every 30-min run, so the same signal instance
    # (same pair + same bar it was generated on) appears repeatedly until the
    # underlying bar advances. Key on (pair, window.generated_at_utc) — that's
    # the actual signal instance, not each time it happened to be recomputed.
    seen = {}
    for e in entries:
        key = (e["pair"], e.get("window", {}).get("generated_at_utc") or e["logged_at"])
        seen[key] = e  # last write wins — SL/TP/entry are stable per instance anyway
    return list(seen.values())


def score_signal(entry: dict) -> dict:
    pair = entry["pair"]
    direction = 1 if entry["direction"] == "long" else -1
    sl_lo, sl_hi = entry["stop_loss_range"]
    tp_lo, tp_hi = entry["take_profit_range"]
    # combiner.py: for long, sl_range=(entry-sl_far, entry-sl_near) so sl_hi is
    # the NEARER (more conservative) stop; for short, sl_range=(entry+sl_near,
    # entry+sl_far) so sl_lo is nearer. Use the conservative (nearer) stop.
    stop = sl_hi if direction == 1 else sl_lo
    # tp_range: for long, (entry+tp_near, entry+tp_far) -> tp_hi is the full target;
    # for short, (entry-tp_far, entry-tp_near) -> tp_lo is the full target.
    target = tp_hi if direction == 1 else tp_lo

    window = entry.get("window") or {}
    start_str = window.get("generated_at_utc") or entry["logged_at"]
    start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    end = min(start + timedelta(hours=MAX_LOOKAHEAD_HOURS), datetime.now(timezone.utc))

    result = dict(entry, stop=stop, target=target, outcome="unresolved", exit_price=None, r_multiple=None)
    if end <= start:
        return result

    df = load_real_ohlc(pair, start, end, freq="30min")
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
        if hit_stop and hit_target:
            result["outcome"], result["exit_price"] = "stop", stop  # conservative, same convention as backtest.py
            break
        if hit_stop:
            result["outcome"], result["exit_price"] = "stop", stop
            break
        if hit_target:
            result["outcome"], result["exit_price"] = "target", target
            break

    if result["outcome"] in ("stop", "target"):
        result["r_multiple"] = direction * (result["exit_price"] - entry_price) / risk
    return result


def render_brief_html(date_str: str, scored: list[dict]) -> str:
    resolved = [s for s in scored if s["outcome"] in ("stop", "target")]
    wins = [s for s in resolved if s["outcome"] == "target"]
    win_rate = len(wins) / len(resolved) if resolved else None
    avg_r = sum(s["r_multiple"] for s in resolved) / len(resolved) if resolved else None

    rows_html = ""
    for s in scored:
        badge = {"target": "TARGET HIT", "stop": "STOPPED OUT", "unresolved": "STILL OPEN", "no_data": "NO DATA"}[s["outcome"]]
        badge_cls = {"target": "good", "stop": "bad", "unresolved": "neutral", "no_data": "neutral"}[s["outcome"]]
        r_str = f"{s['r_multiple']:+.2f}R" if s.get("r_multiple") is not None else "—"
        rows_html += f"""
        <tr>
          <td>{s['pair']}</td><td>{s['direction'].upper()}</td><td>{s['confidence']}</td>
          <td class="{badge_cls}">{badge}</td><td>{r_str}</td>
          <td class="reason">{s['reason']}</td>
        </tr>"""

    summary = (
        f"{len(scored)} signal(s) fired, {len(resolved)} resolved. "
        + (f"Win rate: {win_rate:.0%}, avg {avg_r:+.2f}R." if resolved else "None resolved yet.")
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/><title>FX End-of-Day Brief — {date_str}</title>
<style>
  body {{ font-family: system-ui,-apple-system,sans-serif; background:#f9f9f7; color:#0b0b0b; margin:0; }}
  .wrap {{ max-width: 900px; margin:0 auto; padding: 32px 20px 64px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .subtitle {{ color:#52514e; font-size:13px; margin-bottom:20px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; background:#fcfcfb; border:1px solid rgba(11,11,11,0.10); border-radius:10px; overflow:hidden; }}
  th, td {{ padding:8px 10px; text-align:left; border-bottom:1px solid rgba(11,11,11,0.08); }}
  th {{ background:#f1f0ec; font-size:11px; text-transform:uppercase; color:#898781; }}
  .good {{ color:#0ca30c; font-weight:600; }}
  .bad {{ color:#d03b3b; font-weight:600; }}
  .neutral {{ color:#898781; font-weight:600; }}
  .reason {{ color:#898781; font-size:12px; }}
</style></head><body><div class="wrap">
  <h1>FX End-of-Day Brief</h1>
  <div class="subtitle">{date_str} — {summary}</div>
  <table><thead><tr><th>Pair</th><th>Direction</th><th>Confidence</th><th>Outcome</th><th>R</th><th>Reason</th></tr></thead>
  <tbody>{rows_html or '<tr><td colspan="6">No signals fired today.</td></tr>'}</tbody></table>
</div></body></html>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, default=None)
    args = parser.parse_args()
    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    entries = load_day_signals(date_str)
    print(f"{len(entries)} unique signal instance(s) logged for {date_str}.")
    scored = [score_signal(e) for e in entries]

    for s in scored:
        print(f"  {s['pair']} {s['direction']} @ {s['entry']:.5f} -> {s['outcome']}"
              + (f" ({s['r_multiple']:+.2f}R)" if s.get("r_multiple") is not None else ""))

    html = render_brief_html(date_str, scored)
    out_path = Path(__file__).parent / "eod_brief.html"
    out_path.write_text(html)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
