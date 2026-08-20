"""Combines technical score + PESTLE score into a final trade signal with
confidence tier and ATR-based stop-loss / take-profit range.

See MODEL_SPEC.md §5.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime

ALPHA = 0.6  # weight on technical vs PESTLE
AGREEMENT_THRESHOLD = 0.3
VETO_THRESHOLD = 0.4
CONFIDENCE_HIGH = 0.6
CONFIDENCE_MEDIUM = 0.35
STRONG_AGREEMENT = 0.75


@dataclass
class TradeSignal:
    pair: str
    direction: str          # "long" | "short" | "no_trade"
    confidence: str         # "high" | "medium" | "low"
    combined_score: float
    tech_score: float
    pestle_score: float
    entry: float
    stop_loss_range: tuple[float, float]
    take_profit_range: tuple[float, float]
    reason: str
    window: dict | None = None  # from sessions.signal_window(): session, generated_at, valid_until (GMT/UTC)


def combine_signal(pair: str, entry: float, atr_value: float, tech_score: float, pestle_score: float,
                    alpha: float = ALPHA, generated_at: datetime | None = None) -> TradeSignal:
    window = None
    if generated_at is not None:
        from sessions import signal_window
        window = signal_window(pair, generated_at)

    # Disagreement veto: strong conflicting signals -> no trade
    if (tech_score * pestle_score < 0) and abs(tech_score) > VETO_THRESHOLD and abs(pestle_score) > VETO_THRESHOLD:
        return TradeSignal(
            pair=pair, direction="no_trade", confidence="low", combined_score=0.0,
            tech_score=tech_score, pestle_score=pestle_score, entry=entry,
            stop_loss_range=(entry, entry), take_profit_range=(entry, entry),
            reason="Technical and PESTLE signals strongly disagree — vetoed.",
            window=window,
        )

    combined = alpha * tech_score + (1 - alpha) * pestle_score
    combined = max(-1.0, min(1.0, combined))
    agree = (tech_score * pestle_score > 0) and abs(tech_score) > AGREEMENT_THRESHOLD and abs(pestle_score) > AGREEMENT_THRESHOLD

    magnitude = abs(combined)
    if magnitude >= CONFIDENCE_HIGH:
        confidence = "high"
    elif magnitude >= CONFIDENCE_MEDIUM:
        confidence = "medium"
    else:
        confidence = "low"

    if magnitude < CONFIDENCE_MEDIUM:
        direction = "no_trade"
    else:
        direction = "long" if combined > 0 else "short"

    # ATR-based SL/TP ranges (MODEL_SPEC.md §5.1)
    rr = 2.0 if (confidence == "high" and magnitude >= STRONG_AGREEMENT) else 1.5
    sl_near, sl_far = 0.8 * atr_value, 1.2 * atr_value
    tp_near, tp_far = 1.3 * atr_value, (rr / 1.5) * 1.5 * atr_value  # scales TP range with rr

    if direction == "long":
        sl_range = (entry - sl_far, entry - sl_near)
        tp_range = (entry + tp_near, entry + tp_far)
    elif direction == "short":
        sl_range = (entry + sl_near, entry + sl_far)
        tp_range = (entry - tp_far, entry - tp_near)
    else:
        sl_range = (entry, entry)
        tp_range = (entry, entry)

    reason = (
        f"tech={tech_score:+.2f}, pestle={pestle_score:+.2f}, combined={combined:+.2f}"
        + (" (agreement boost)" if agree else "")
    )

    return TradeSignal(
        pair=pair, direction=direction, confidence=confidence, combined_score=combined,
        tech_score=tech_score, pestle_score=pestle_score, entry=entry,
        stop_loss_range=sl_range, take_profit_range=tp_range, reason=reason,
        window=window,
    )
