"""Walk-forward replay engine. For each (candidate, pair), computes the
candidate's H4 signal series once (all three underlying indicator sets --
EMA/ATR/MACD-based trend, ATR-based pattern, and either the existing
composite orb_signal or this milestone's genuine/absent replacements --
are causal/trailing-window functions, so evaluating them over the full
series and then reading bar ts's own value is numerically identical to
computing them incrementally up to ts; this is stated explicitly rather
than assumed silently), then walks H4 bars in chronological order,
applying the position policy and lifecycle bookkeeping from configs.py.

Every alert version is recorded via alert_lifecycle.classify_and_record
(reused unmodified from the alert-lifecycle branch, per "use the existing
lifecycle code where appropriate") and resolved with replay_scorer.py.
Every ledger row is tagged hypothetical=True and result_type=
"hypothetical_replay" -- nothing here is presented as a real subscriber
having received or acted on anything.
"""
from __future__ import annotations
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "strategies"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from data_loader import PairData, WARMUP_START  # noqa: E402
import configs as cfg  # noqa: E402
from replay_scorer import score_replay_version  # noqa: E402
from alert_lifecycle import AlertLifecycleStore, classify_and_record, guarded_entry_expiry  # noqa: E402
from combiner import combine_signal  # noqa: E402  (the ACTUAL frozen production combiner -- see _combiner_levels)

from strategies.composite import technical_score  # noqa: E402  (existing, unmodified baseline)
from genuine_orb import genuine_orb_technical_score  # noqa: E402
from trend_pullback import trend_pullback_levels  # noqa: E402


def _combiner_levels(pair: str, tech_row: pd.Series, entry_price: float, atr_value: float) -> dict | None:
    """Calls the ACTUAL frozen production combine_signal() (src/combiner.py),
    not a hand-rolled reimplementation of its thresholds/formula. The
    earlier version of this function reimplemented the confidence tiers
    and SL/TP formula by hand and, in doing so, silently omitted the
    spread floor (MIN_RISK_SPREAD_MULTIPLE) combine_signal() applies --
    corrected by calling the real function directly, so parity with
    production is guaranteed by construction rather than approximated.
    Used for both baseline and genuine_orb (both reuse the same combiner;
    only their tech_score input differs). alpha=1.0 and pestle_score=0.0
    match live_scanner.py's actual deployed configuration exactly (tech-
    only, no PESTLE) -- NOT combiner.py's own default alpha=0.6."""
    score = tech_row["tech_score"]
    if pd.isna(score) or pd.isna(atr_value) or atr_value == 0:
        return None
    sig = combine_signal(pair=pair, entry=entry_price, atr_value=atr_value,
                          tech_score=float(score), pestle_score=0.0, alpha=1.0, generated_at=None)
    if sig.direction == "no_trade":
        return None
    sl_lo, sl_hi = sig.stop_loss_range
    tp_lo, tp_hi = sig.take_profit_range
    stop, target = (sl_hi, tp_hi) if sig.direction == "long" else (sl_lo, tp_lo)
    return {"direction": sig.direction, "confidence": sig.confidence, "combined_score": float(sig.combined_score),
            "entry": entry_price, "stop": stop, "target": target, "atr": atr_value}


def compute_signal_series(candidate: str, pair_data: PairData) -> pd.DataFrame:
    """One row per H4 bar: direction/confidence/combined_score/entry/stop/target/atr
    (direction='no_trade' where nothing fires), plus technical_inputs dict for the ledger."""
    h4_mid = pair_data.h4_mid
    if candidate == "baseline":
        tech = technical_score(h4_mid)
    elif candidate == "genuine_orb":
        tech = genuine_orb_technical_score(h4_mid, _m30_mid(pair_data), pair_data.pair)
    else:
        raise ValueError(candidate)

    rows = []
    for ts, bar in h4_mid.iterrows():
        tech_row = tech.loc[ts]
        levels = _combiner_levels(pair_data.pair, tech_row, entry_price=float(bar["close"]), atr_value=float(tech_row["atr"]))
        if levels is None:
            rows.append({"direction": "no_trade", "confidence": None, "combined_score": float(tech_row["tech_score"]) if not pd.isna(tech_row["tech_score"]) else 0.0,
                          "entry": None, "stop": None, "target": None, "atr": float(tech_row["atr"]) if not pd.isna(tech_row["atr"]) else None,
                          "technical_inputs": {"orb": float(tech_row["orb"]), "trend": float(tech_row["trend"]), "pattern": float(tech_row["pattern"])}})
        else:
            rows.append({**levels, "technical_inputs": {"orb": float(tech_row["orb"]), "trend": float(tech_row["trend"]), "pattern": float(tech_row["pattern"])}})
    return pd.DataFrame(rows, index=h4_mid.index)


def _m30_mid(pair_data: PairData) -> pd.DataFrame:
    b, a = pair_data.m30_bid, pair_data.m30_ask
    return pd.DataFrame({"open": (b["open"] + a["open"]) / 2, "high": (b["high"] + a["high"]) / 2,
                          "low": (b["low"] + a["low"]) / 2, "close": (b["close"] + a["close"]) / 2}, index=b.index)


def compute_trend_pullback_series(pair_data: PairData) -> pd.DataFrame:
    levels = trend_pullback_levels(pair_data.h4_mid)
    rows = []
    for ts, row in levels.iterrows():
        if row["direction"] == "no_trade" or pd.isna(row["stop"]):
            rows.append({"direction": "no_trade", "confidence": None, "combined_score": 0.0,
                          "entry": None, "stop": None, "target": None, "atr": float(row["atr"]) if not pd.isna(row["atr"]) else None,
                          "technical_inputs": {}})
        else:
            rows.append({"direction": row["direction"], "confidence": row["confidence"], "combined_score": None,
                          "entry": float(row["entry"]), "stop": float(row["stop"]), "target": float(row["target"]),
                          "atr": float(row["atr"]), "technical_inputs": {}})
    return pd.DataFrame(rows, index=levels.index)


def run_replay(candidate: str, pair_data: PairData, dataset_end: datetime) -> list[dict]:
    """Returns the full ledger for one (candidate, pair): one row per
    lifecycle event (issued/revised/cancelled/suppressed), each carrying
    its replay-scored outcome where applicable."""
    pair = pair_data.pair
    signal_series = (compute_trend_pullback_series(pair_data) if candidate == "trend_pullback"
                      else compute_signal_series(candidate, pair_data))
    h4_index = list(signal_series.index)
    warmup_cutoff_idx = min(cfg.WARMUP_H4_BARS, len(h4_index))
    eligible_index = h4_index[warmup_cutoff_idx:]

    store = AlertLifecycleStore(Path("/tmp") / f"replay_{candidate}_{pair}_{id(pair_data)}.jsonl")
    if store.path.exists():
        store.path.unlink()

    ledger: list[dict] = []
    open_position: dict | None = None  # {"exit_time": datetime, "conservative_occupancy": bool} while unresolved
    h4_duration = cfg.CANDLE_DURATIONS["H4"]

    for ts in eligible_index:
        row = signal_series.loc[ts]
        # ts is the SOURCE H4 CANDLE'S OWN OPEN (OANDA candle `time` = open
        # time). Its close/high/low/tech_score are only knowable once that
        # candle has actually completed -- decision_time is that
        # completion, computed directly (open + fixed duration), never
        # inferred from the next available row (wrong across a data gap).
        # This contract's execution delay is 0, so decision_time IS the
        # earliest permitted entry time -- kept as a separate named value
        # regardless, so the two concepts are never silently conflated.
        source_candle_start = ts.to_pydatetime()
        source_candle_completion = source_candle_start + h4_duration
        decision_time = source_candle_completion
        earliest_permitted_entry_time = decision_time + timedelta(minutes=cfg.EXECUTION_DELAY_MINUTES)
        timing_fields = {
            "source_candle_start_utc": source_candle_start.isoformat(),
            "source_candle_completion_utc": source_candle_completion.isoformat(),
            "decision_time_utc": decision_time.isoformat(),
            "earliest_permitted_entry_time_utc": earliest_permitted_entry_time.isoformat(),
        }

        if open_position is not None:
            if open_position["exit_time"] is not None and open_position["exit_time"] <= decision_time:
                open_position = None
            else:
                ledger.append({
                    "pair": pair, "candidate": candidate, "event_type": "suppressed_existing_position",
                    "recorded_at_utc": decision_time.isoformat(), "hypothetical": True,
                    "alert_id": None, "lineage_id": None, "direction": row["direction"],
                    "conservative_occupancy": open_position.get("conservative_occupancy", False),
                    **timing_fields,
                })
                continue

        if row["direction"] == "no_trade" or row["entry"] is None:
            continue

        atr_value = row["atr"] if row["atr"] not in (None,) and not pd.isna(row["atr"]) else None
        if atr_value is None or atr_value == 0:
            continue

        events = classify_and_record(
            store, scanner_version=cfg.CANDIDATES[candidate].scanner_version, pair=pair,
            direction=row["direction"], confidence=row["confidence"] or "medium",
            combined_score=row["combined_score"] if row["combined_score"] is not None else 0.0,
            entry_price=row["entry"], atr_value=atr_value, stop=row["stop"], target=row["target"],
            technical_inputs=row["technical_inputs"], pestle_inputs=None, pestle_used=False,
            reason=f"{candidate} replay signal", calculated_at=decision_time, published_at=earliest_permitted_entry_time,
            entry_tolerance=cfg.ENTRY_TOLERANCE_ATR_MULTIPLE * atr_value,
            entry_validity_minutes=int(cfg.ENTRY_VALIDITY_HOURS * 60),
            max_holding_time_hours=cfg.MAX_HOLDING_TIME_HOURS,
        )

        for event in events:
            event["hypothetical"] = True
            event.update(timing_fields)
            if event["event_type"] != "issued" and event["event_type"] != "revised":
                ledger.append({**event, "candidate": candidate, "state": event.get("event_type")})
                continue

            version = {
                "pair": pair, "direction": event["direction"], "entry": event["entry_price"],
                "entry_condition_lo": event["entry_condition_lo"], "entry_condition_hi": event["entry_condition_hi"],
                "stop": event["stop"], "target": event["target"],
                "published_at": datetime.fromisoformat(event["recorded_at_utc"]),
                "max_holding_time_hours": event["max_holding_time_hours"],
            }
            window_end = min(datetime.fromisoformat(event["entry_expiry_utc"]),
                              earliest_permitted_entry_time + timedelta(hours=cfg.ENTRY_VALIDITY_HOURS))
            scored = score_replay_version(version, window_end, "entry_expiry", pair_data.m30_bid, pair_data.m30_ask, now=dataset_end)
            ledger.append({**event, "candidate": candidate, **scored})

            entered = scored.get("assumed_entry_time_utc") is not None
            if entered:
                if scored.get("exit_time_utc"):
                    exit_dt = datetime.fromisoformat(scored["exit_time_utc"])
                    conservative_occupancy = False
                else:
                    # entered but no clean exit_time_utc: ambiguous_intrabar_exit,
                    # incomplete_coverage, or still 'open' at dataset_end. An
                    # ambiguous/incomplete outcome must not silently free the
                    # pair for a new entry -- conservatively reserve it for the
                    # full stated holding window from entry, since we cannot
                    # confirm when (or whether) it actually resolved. This
                    # reduces this pair's opportunity count versus treating it
                    # as immediately free; flagged via conservative_occupancy
                    # on every suppressed row this produces, and summarised in
                    # RESULTS.md.
                    entry_dt = datetime.fromisoformat(scored["assumed_entry_time_utc"])
                    exit_dt = entry_dt + timedelta(hours=version["max_holding_time_hours"])
                    conservative_occupancy = True
                if exit_dt > decision_time:
                    open_position = {"exit_time": exit_dt, "conservative_occupancy": conservative_occupancy}

    if store.path.exists():
        store.path.unlink()
    return ledger
