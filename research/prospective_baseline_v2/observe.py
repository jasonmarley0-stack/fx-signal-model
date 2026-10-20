"""v2 prospective baseline collector. Decision-tick logic (what the FROZEN
baseline would decide) is BYTE-IDENTICAL to v1 -- same technical_score(),
same combine_signal(), same alpha=1.0/pestle_score=0.0, same entry-tolerance
and entry-validity computation. Only the quote tick changed, to match
MEASUREMENT_CONTRACT.md: it now calls v2's bounded-retry
fetch_pricing_samples() and logs every attempt (success or failure) to
health_log, not just a single pass/fail per tick.

Writes to its OWN isolated log directory
(research/prospective_baseline_v2/logs/) -- never to v1's logs/, never to
signals_log/, alerts.json, performance.json, or any file the public
scanner/dashboard reads.

Usage (NOT started by this change):
    set -a; source setup/oanda.env; set +a
    .venv/bin/python3 research/prospective_baseline_v2/observe.py
"""
from __future__ import annotations
import json
import sys
import threading
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
    """Unchanged from v1, except a lock guarding each append(): the
    decision-tick and quote-tick loops now run on separate threads (see
    main()) and can both write to health_log concurrently. A single
    small write() is atomic at the OS level under O_APPEND in practice,
    but this makes that guarantee explicit rather than relying on it."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, row: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a") as f:
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
    for row in decisions_log.read_all():
        if row.get("event_type") == "decision" and row.get("pair") == pair \
                and row.get("source_candle_completion_utc") == source_candle_completion_utc:
            return True
    return False


def compute_decision(pair: str, h4_mid, now: datetime) -> dict | None:
    """BYTE-IDENTICAL logic to v1 -- calls the same frozen technical_score()/
    combine_signal(), same config. See MEASUREMENT_CONTRACT.md section 5:
    v2 does not touch signal generation."""
    if len(h4_mid) < 60:
        return None
    tech = technical_score(h4_mid)
    latest_ts = h4_mid.index[-1]
    tech_row = tech.loc[latest_ts]
    score = tech_row["tech_score"]
    atr_value = tech_row["atr"]
    if score != score or atr_value != atr_value or atr_value == 0:
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
    """Unchanged from v1."""
    recorded = []
    for pair in pairs:
        calc_start = now_fn()
        try:
            h4_mid = fetch_h4_fn(pair)
        except Exception as ex:  # noqa: BLE001
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
        source_candle_completion = source_candle_start + timedelta(hours=4)

        if decision_already_recorded(decisions_log, pair, source_candle_completion.isoformat()):
            continue

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
            "hypothetical": True, "result_type": "prospective_paper_v2", "contract_version": "v2",
        }
        decisions_log.append(row)
        recorded.append(row)
        state[pair] = source_candle_completion.isoformat()
    return recorded


def run_quote_tick(pairs: list[str], quotes_log: AppendLog, health_log: AppendLog, fetch_pricing_fn,
                    now_fn=lambda: datetime.now(timezone.utc)) -> list[dict]:
    """v2: `fetch_pricing_fn(pairs) -> {"samples": [...], "attempts": [...]}`
    (quote_client.fetch_pricing_samples's new bounded-retry shape). Every
    attempt is logged to health_log as its own event -- success or
    failure, auth or transient or maintenance -- real evidence of request
    activity, not just a single pass/fail per tick as in v1. Still never
    raises; a total failure after all retries records the attempts and
    returns, same "never stop the collector" discipline as v1."""
    tick_started_at = now_fn()
    try:
        result = fetch_pricing_fn(pairs)
    except Exception as ex:  # noqa: BLE001 -- a genuine programming error (e.g. missing account id), not a request failure
        return [health_log.append({"event_type": "quote_tick_crashed", "recorded_at_utc": tick_started_at.isoformat(),
                                   "error": str(ex)})]
    recorded = []
    for a in result.get("attempts", []):
        recorded.append(health_log.append({"event_type": "quote_attempt", "recorded_at_utc": tick_started_at.isoformat(), **a}))
    samples = result.get("samples", [])
    for s in samples:
        quotes_log.append(s)
    return recorded + samples


def _fetch_rolling_h4_mid(pair: str):  # pragma: no cover
    return fetch_oanda_candles(pair, granularity=cfg.DECISION_GRANULARITY, count=cfg.ROLLING_HISTORY_BARS,
                                timeout=cfg.REQUEST_TIMEOUT_SECONDS)


def _decision_tick_loop(decisions_log: AppendLog, health_log: AppendLog, state: dict, fetch_h4_fn,
                        stop_event: "threading.Event", poll_interval_seconds: float | None = None,
                        monotonic_fn=time.monotonic, wait_fn=None) -> None:
    """Runs decision ticks (an H4 fetch + compute_decision() per pair,
    now individually timeout-bounded -- see _fetch_rolling_h4_mid) on
    their OWN cadence, on a thread separate from quote sampling. This is
    the collector-blocking fix: previously a single shared loop ran
    run_quote_tick() then, every DECISION_POLL_INTERVAL_SECONDS,
    run_decision_tick() inline before the next quote sample -- a ~12s
    decision cycle (7 sequential, previously UNBOUNDED H4 requests) plus
    the subsequent QUOTE_SAMPLE_INTERVAL_SECONDS sleep created a ~17s
    receipt gap on every single decision tick, which COVERAGE_GAP_SECONDS
    (15s, zero-tolerance) treats as permanently disqualifying -- the
    confirmed root cause of v2's zero completed trades (see the 2026-10
    diagnosis). Decision-tick work can now take arbitrarily long (bounded
    only by its own per-request timeout) without ever again delaying the
    next quote sample, which keeps ticking on _quote_tick_loop's own
    thread regardless."""
    poll_interval_seconds = cfg.DECISION_POLL_INTERVAL_SECONDS if poll_interval_seconds is None else poll_interval_seconds
    wait_fn = wait_fn or stop_event.wait
    while not stop_event.is_set():
        tick_start = monotonic_fn()
        try:
            run_decision_tick(cfg.PAIRS, decisions_log, health_log, state, fetch_h4_fn)
            save_state(state)
        except Exception:  # noqa: BLE001
            health_log.append({"event_type": "decision_tick_crashed", "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                               "traceback": traceback.format_exc()})
        elapsed = monotonic_fn() - tick_start
        wait_fn(max(0.0, poll_interval_seconds - elapsed))


def _quote_tick_loop(quotes_log: AppendLog, health_log: AppendLog, fetch_pricing_fn, stop_event: "threading.Event",
                     sample_interval_seconds: float | None = None, wait_fn=None) -> None:
    """The primary sampling loop: ticks on a fixed cadence, unaffected by
    however long a concurrently-running decision tick takes (see
    _decision_tick_loop, run on its own thread by main())."""
    sample_interval_seconds = cfg.QUOTE_SAMPLE_INTERVAL_SECONDS if sample_interval_seconds is None else sample_interval_seconds
    wait_fn = wait_fn or stop_event.wait
    while not stop_event.is_set():
        try:
            run_quote_tick(cfg.PAIRS, quotes_log, health_log, fetch_pricing_fn)
        except Exception:  # noqa: BLE001
            health_log.append({"event_type": "quote_tick_crashed", "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                               "traceback": traceback.format_exc()})
        wait_fn(sample_interval_seconds)


def main() -> None:  # pragma: no cover
    enforce_practice_environment()
    manifest = load_or_create_manifest(LOG_DIR)
    print(f"v2 run identity: run_id={manifest['run_id']} source_hash={manifest['source_hash'][:12]}...")

    decisions_log = AppendLog(DECISIONS_LOG)
    quotes_log = AppendLog(QUOTES_LOG)
    health_log = AppendLog(HEALTH_LOG)
    state = load_state()

    stop_event = threading.Event()
    decision_thread = threading.Thread(
        target=_decision_tick_loop, args=(decisions_log, health_log, state, _fetch_rolling_h4_mid, stop_event),
        name="v2-decision-tick", daemon=True)
    decision_thread.start()

    print(f"Prospective baseline observation v2 starting (practice-only, enforced). Logs: {LOG_DIR}")
    try:
        _quote_tick_loop(quotes_log, health_log, fetch_pricing_samples, stop_event)
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
