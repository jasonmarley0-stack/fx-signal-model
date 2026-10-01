"""The prospective baseline collector. Records, on fresh live data, what
the FROZEN baseline (src/strategies/composite.py + src/combiner.py,
called directly, not reimplemented) would decide at each H4 candle's
completion, plus a real timestamped bid/ask quote sample stream for
scoring those decisions later (score.py). Every recorded decision and
quote is explicitly hypothetical -- this writes to its OWN isolated log
directory only, never to signals_log/, alerts.json, performance.json, or
any file the public scanner/dashboard reads.

Two independent cadences within one process (see contract.py):
  - a decision tick every DECISION_POLL_INTERVAL_SECONDS, checking each
    pair for a newly-completed H4 candle and recording the baseline's
    decision on it if one exists and hasn't been recorded yet;
  - a quote tick every QUOTE_SAMPLE_INTERVAL_SECONDS, recording real
    bid/ask for every pair.

Restart-safe: decisions are deduplicated against the actual log (not only
the state file), so a crash/restart never double-records a decision for a
candle already decided on.

Usage (NOT started by this change -- see README.md for the practice-only
smoke check and supervision instructions):
    set -a; source setup/oanda.env; set +a
    .venv/bin/python3 research/prospective_baseline/observe.py
"""
from __future__ import annotations
import json
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import contract as cfg  # noqa: E402
from quote_client import fetch_pricing_samples  # noqa: E402
from strategies.composite import technical_score  # noqa: E402
from combiner import combine_signal  # noqa: E402
from data.oanda import fetch_oanda_candles  # noqa: E402
from alert_lifecycle import guarded_entry_expiry  # noqa: E402
from run_identity import enforce_practice_environment, load_or_create_manifest  # noqa: E402

LOG_DIR = Path(__file__).parent / "logs"
DECISIONS_LOG = LOG_DIR / "decisions_log.jsonl"
QUOTES_LOG = LOG_DIR / "quotes_log.jsonl"
HEALTH_LOG = LOG_DIR / "health_log.jsonl"
STATE_PATH = LOG_DIR / ".observe_state.json"


class AppendLog:
    """Generic append-only JSONL log -- the same discipline
    src/alert_lifecycle.py's AlertLifecycleStore uses, reused here rather
    than reinvented."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, row: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return row

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out


def load_state(path: Path = STATE_PATH) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def decision_already_recorded(decisions_log: AppendLog, pair: str, source_candle_completion_utc: str) -> bool:
    """Checked against the ACTUAL log, not only the in-memory/persisted
    state -- true restart-safety even if the state file is lost or stale
    (e.g. restored from an older backup)."""
    for row in decisions_log.read_all():
        if row.get("event_type") == "decision" and row.get("pair") == pair \
                and row.get("source_candle_completion_utc") == source_candle_completion_utc:
            return True
    return False


def compute_decision(pair: str, h4_mid, now: datetime) -> dict | None:
    """Reuses the real, frozen technical_score()/combine_signal() --
    exactly src/combiner.py's spread floor and thresholds, exactly
    live_scanner.py's alpha=1.0/pestle_score=0.0 configuration. `h4_mid`
    is a rolling-window OHLC DataFrame (real caller: ROLLING_HISTORY_BARS
    fresh bars from OANDA; test caller: an injected fixture). Returns the
    decision for the LATEST bar in h4_mid, or None if that bar produces
    no_trade or indicators aren't warm yet."""
    if len(h4_mid) < 60:
        return None
    tech = technical_score(h4_mid)
    latest_ts = h4_mid.index[-1]
    tech_row = tech.loc[latest_ts]
    score = tech_row["tech_score"]
    atr_value = tech_row["atr"]
    if score != score or atr_value != atr_value or atr_value == 0:  # NaN check without importing pandas here
        return None
    entry_price = float(h4_mid["close"].iloc[-1])
    sig = combine_signal(pair=pair, entry=entry_price, atr_value=float(atr_value), tech_score=float(score),
                          pestle_score=0.0, alpha=1.0, generated_at=None)
    if sig.direction == "no_trade":
        return None
    sl_lo, sl_hi = sig.stop_loss_range
    tp_lo, tp_hi = sig.take_profit_range
    stop, target = (sl_hi, tp_hi) if sig.direction == "long" else (sl_lo, tp_lo)
    return {
        "source_candle_start_utc": latest_ts.to_pydatetime().isoformat(),
        "direction": sig.direction, "confidence": sig.confidence, "combined_score": float(sig.combined_score),
        "entry_price": entry_price, "stop": stop, "target": target, "atr_value": float(atr_value),
        "technical_inputs": {"orb": float(tech_row["orb"]), "trend": float(tech_row["trend"]), "pattern": float(tech_row["pattern"])},
    }


def run_decision_tick(pairs: list[str], decisions_log: AppendLog, health_log: AppendLog, state: dict,
                       fetch_h4_fn, now_fn=lambda: datetime.now(timezone.utc)) -> list[dict]:
    """`fetch_h4_fn(pair) -> mid_ohlc DataFrame` is injected so this is
    fully testable without network/credentials. Returns every row
    appended this tick (decisions and/or health/failure events)."""
    recorded = []
    for pair in pairs:
        calc_start = now_fn()
        try:
            h4_mid = fetch_h4_fn(pair)
        except Exception as ex:  # noqa: BLE001 — one pair's failure must not stop the others
            recorded.append(health_log.append({
                "event_type": "poll_failed", "pair": pair, "recorded_at_utc": calc_start.isoformat(),
                "error": str(ex),
            }))
            continue

        if h4_mid is None or len(h4_mid) == 0:
            recorded.append(health_log.append({
                "event_type": "no_data", "pair": pair, "recorded_at_utc": calc_start.isoformat(),
            }))
            continue

        latest_ts = h4_mid.index[-1]
        source_candle_start = latest_ts.to_pydatetime()
        source_candle_completion = source_candle_start + timedelta(hours=4)  # H4's own fixed duration, same discipline as the offline replay

        if decision_already_recorded(decisions_log, pair, source_candle_completion.isoformat()):
            continue  # restart-safe: this candle was already decided on, do not duplicate

        decision = compute_decision(pair, h4_mid, calc_start)
        actual_calculation_time = now_fn()
        decision_delay_seconds = (actual_calculation_time - source_candle_completion).total_seconds()

        if decision is None:
            recorded.append(health_log.append({
                "event_type": "no_signal", "pair": pair,
                "source_candle_start_utc": source_candle_start.isoformat(),
                "source_candle_completion_utc": source_candle_completion.isoformat(),
                "recorded_at_utc": actual_calculation_time.isoformat(),
                "decision_delay_seconds": decision_delay_seconds,
            }))
            state[pair] = source_candle_completion.isoformat()
            continue

        entry_expiry = guarded_entry_expiry(pair, actual_calculation_time, int(cfg.ENTRY_VALIDITY_HOURS * 60))
        actual_recording_time = now_fn()
        tol = cfg.ENTRY_TOLERANCE_ATR_MULTIPLE * decision["atr_value"]
        row = {
            "event_type": "decision", "pair": pair,
            "source_candle_start_utc": decision["source_candle_start_utc"],
            "source_candle_completion_utc": source_candle_completion.isoformat(),
            "actual_calculation_time_utc": actual_calculation_time.isoformat(),
            "actual_recording_time_utc": actual_recording_time.isoformat(),
            "decision_delay_seconds": decision_delay_seconds,
            "direction": decision["direction"], "confidence": decision["confidence"],
            "combined_score": decision["combined_score"], "entry_price": decision["entry_price"],
            "stop": decision["stop"], "target": decision["target"],
            "entry_condition_lo": decision["entry_price"] - tol,
            "entry_condition_hi": decision["entry_price"] + tol,
            "entry_expiry_utc": entry_expiry.isoformat(),
            "max_holding_time_hours": cfg.MAX_HOLDING_TIME_HOURS,
            "technical_inputs": decision["technical_inputs"],
            "hypothetical": True, "result_type": "prospective_paper",
        }
        decisions_log.append(row)
        recorded.append(row)
        state[pair] = source_candle_completion.isoformat()
    return recorded


def run_quote_tick(pairs: list[str], quotes_log: AppendLog, health_log: AppendLog, fetch_pricing_fn,
                    now_fn=lambda: datetime.now(timezone.utc)) -> list[dict]:
    """`fetch_pricing_fn(pairs) -> list[dict]` is injected for testability;
    it is responsible for stamping its OWN received_at_utc on each sample
    (see quote_client.fetch_pricing_samples, which does this after its
    real network response returns). This tick's own `now_fn()` is used
    ONLY for this tick's bookkeeping (the failure-event timestamp below)
    -- it must never be threaded into a sample's received_at_utc, or it
    silently reintroduces the pre-request timing defect the pricing
    client's own post-response capture fixed. One failure records a
    health event and does not raise -- the collector must keep running."""
    tick_started_at = now_fn()
    try:
        samples = fetch_pricing_fn(pairs)
    except Exception as ex:  # noqa: BLE001
        return [health_log.append({"event_type": "quote_poll_failed", "recorded_at_utc": tick_started_at.isoformat(), "error": str(ex)})]
    for s in samples:
        quotes_log.append(s)
    return samples


def _fetch_rolling_h4_mid(pair: str):  # pragma: no cover — thin real-network wrapper, exercised only manually/live
    """Real caller only: fetch_oanda_candles() already returns midpoint
    OHLC (price="M"), matching exactly what technical_score()/combine_signal()
    expect and what live_scanner.py itself feeds them — no separate
    mid-price conversion needed here, unlike the offline replay's
    bid/ask dataset."""
    return fetch_oanda_candles(pair, granularity=cfg.DECISION_GRANULARITY, count=cfg.ROLLING_HISTORY_BARS)


def main() -> None:  # pragma: no cover — the real, persistent, network-calling loop; not exercised by tests
    # Activation integrity (requirement 6): enforced in code, not only
    # README instructions. Refuses to start against anything but
    # OANDA_ENVIRONMENT=practice, and refuses to silently continue an
    # existing run's logs under changed code/contract.
    enforce_practice_environment()
    manifest = load_or_create_manifest(LOG_DIR)
    print(f"Run identity: run_id={manifest['run_id']} source_hash={manifest['source_hash'][:12]}...")

    decisions_log = AppendLog(DECISIONS_LOG)
    quotes_log = AppendLog(QUOTES_LOG)
    health_log = AppendLog(HEALTH_LOG)
    state = load_state()

    last_decision_tick = 0.0
    print(f"Prospective baseline observation starting (practice-only, enforced). Logs: {LOG_DIR}")
    while True:
        now = time.monotonic()
        try:
            run_quote_tick(cfg.PAIRS, quotes_log, health_log, fetch_pricing_samples)
        except Exception:  # noqa: BLE001 — never let the loop die
            health_log.append({"event_type": "quote_tick_crashed", "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                               "traceback": traceback.format_exc()})
        if now - last_decision_tick >= cfg.DECISION_POLL_INTERVAL_SECONDS:
            try:
                run_decision_tick(cfg.PAIRS, decisions_log, health_log, state, _fetch_rolling_h4_mid)
                save_state(state)
            except Exception:  # noqa: BLE001
                health_log.append({"event_type": "decision_tick_crashed", "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                                   "traceback": traceback.format_exc()})
            last_decision_tick = now
        time.sleep(cfg.QUOTE_SAMPLE_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
