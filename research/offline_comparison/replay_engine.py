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

from strategies.composite import technical_score  # noqa: E402  (existing, unmodified baseline)
from genuine_orb import genuine_orb_technical_score  # noqa: E402
from trend_pullback import trend_pullback_levels  # noqa: E402


def _confidence_and_rr(magnitude: float) -> tuple[str, float]:
    """Baseline/genuine_orb confidence tiering -- identical to src/combiner.py's
    thresholds (CONFIDENCE_MEDIUM=0.35, CONFIDENCE_HIGH=0.6, STRONG_AGREEMENT=0.75,
    rr=2.0 only if high AND magnitude>=0.75 else 1.5), imported as constants
    here rather than re-importing combiner.py's whole signal-generation path,
    which also needs an `entry`/`atr_value` this replay computes itself."""
    if magnitude >= 0.6:
        confidence = "high"
    elif magnitude >= 0.35:
        confidence = "medium"
    else:
        confidence = "low"
    rr = 2.0 if (confidence == "high" and magnitude >= 0.75) else 1.5
    return confidence, rr


def _baseline_or_orb_levels(tech_row: pd.Series, entry_price: float, atr_value: float) -> dict | None:
    score = tech_row["tech_score"]
    if abs(score) < 0.35 or pd.isna(score) or pd.isna(atr_value) or atr_value == 0:
        return None
    direction = "long" if score > 0 else "short"
    confidence, rr = _confidence_and_rr(abs(score))
    sl_near, sl_far = 1.5 * atr_value, 2.25 * atr_value
    tp_far = rr * atr_value
    if direction == "long":
        stop, target = entry_price - sl_near, entry_price + tp_far
    else:
        stop, target = entry_price + sl_near, entry_price - tp_far
    return {"direction": direction, "confidence": confidence, "combined_score": float(score),
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
        levels = _baseline_or_orb_levels(tech_row, entry_price=float(bar["close"]), atr_value=float(tech_row["atr"]))
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
    open_position: dict | None = None  # {"exit_time": datetime|None, "version": dict} while unresolved

    for ts in eligible_index:
        row = signal_series.loc[ts]
        ts_dt = ts.to_pydatetime()

        if open_position is not None:
            if open_position["exit_time"] is not None and open_position["exit_time"] <= ts_dt:
                open_position = None
            else:
                ledger.append({
                    "pair": pair, "candidate": candidate, "event_type": "suppressed_existing_position",
                    "recorded_at_utc": ts_dt.isoformat(), "hypothetical": True,
                    "alert_id": None, "lineage_id": None, "direction": row["direction"],
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
            reason=f"{candidate} replay signal", calculated_at=ts_dt, published_at=ts_dt,
            entry_tolerance=cfg.ENTRY_TOLERANCE_ATR_MULTIPLE * atr_value,
            entry_validity_minutes=int(cfg.ENTRY_VALIDITY_HOURS * 60),
            max_holding_time_hours=cfg.MAX_HOLDING_TIME_HOURS,
        )

        for event in events:
            event["hypothetical"] = True
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
                              ts_dt + timedelta(hours=cfg.ENTRY_VALIDITY_HOURS))
            scored = score_replay_version(version, window_end, "entry_expiry", pair_data.m30_bid, pair_data.m30_ask, now=dataset_end)
            ledger.append({**event, "candidate": candidate, **scored})

            if scored.get("assumed_entry_time_utc") and scored["state"] in ("open",):
                open_position = {"exit_time": None, "version": event}
            elif scored.get("assumed_entry_time_utc") and scored.get("exit_time_utc"):
                exit_dt = datetime.fromisoformat(scored["exit_time_utc"])
                if exit_dt > ts_dt:
                    open_position = {"exit_time": exit_dt, "version": event}

    if store.path.exists():
        store.path.unlink()
    return ledger
