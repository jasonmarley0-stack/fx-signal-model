"""Performance-analysis functions for the three required views (brief
section 8): A (original live-observed only), B (independently reconstructed
only), C (combined, explicitly labelled mixed-provenance). Pure functions
over lists of trade records -- no file I/O, no network, so they are trivial
to test with synthetic data and safe to run against real data the moment
it exists. Never represents a reconstructed outcome as live-observed: the
caller must pass the correctly-scoped list into each view; this module
does not infer provenance itself.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Trade:
    pair: str
    direction: str  # "long" | "short"
    r_multiple: float | None  # None => unresolved, excluded from every R-based stat
    exit_time_utc: str | None = None  # ISO-8601; used only for drawdown ordering


@dataclass
class PerformanceView:
    label: str
    trade_count: int
    resolved_count: int
    unresolved_count: int
    win_rate: float | None
    total_r: float | None
    average_r: float | None
    profit_factor: float | None
    max_drawdown_r: float | None
    by_pair: dict = field(default_factory=dict)
    by_direction: dict = field(default_factory=dict)


def _resolved(trades: list[Trade]) -> list[Trade]:
    return [t for t in trades if t.r_multiple is not None]


def win_rate(trades: list[Trade]) -> float | None:
    r = _resolved(trades)
    if not r:
        return None
    return sum(1 for t in r if t.r_multiple > 0) / len(r)


def total_r(trades: list[Trade]) -> float | None:
    r = _resolved(trades)
    if not r:
        return None
    return sum(t.r_multiple for t in r)


def average_r(trades: list[Trade]) -> float | None:
    r = _resolved(trades)
    if not r:
        return None
    return sum(t.r_multiple for t in r) / len(r)


def profit_factor(trades: list[Trade]) -> float | None:
    """Sum of winning R / absolute sum of losing R. None if there are no
    resolved trades, or if there are no losses AND no wins either (nothing
    to compute); infinity is reported as None too -- a profit factor of
    "infinity" because there are zero losses is not a usable number for
    aggregate reporting, and is called out explicitly by the caller instead."""
    r = _resolved(trades)
    wins = sum(t.r_multiple for t in r if t.r_multiple > 0)
    losses = sum(-t.r_multiple for t in r if t.r_multiple < 0)
    if not r:
        return None
    if losses == 0:
        return None
    return wins / losses


def max_drawdown_r(trades: list[Trade]) -> float | None:
    """Peak-to-trough on the cumulative-R equity curve, ordered by
    exit_time_utc. Trades without an exit_time_utc are excluded from the
    curve (their contribution to total/average R is still counted
    elsewhere) rather than assumed to sort first or last."""
    r = [t for t in _resolved(trades) if t.exit_time_utc]
    if not r:
        return None
    r = sorted(r, key=lambda t: t.exit_time_utc)
    cum = peak = 0.0
    max_dd = 0.0
    for t in r:
        cum += t.r_multiple
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return max_dd


def _group_stats(trades: list[Trade], key_fn) -> dict:
    groups = defaultdict(list)
    for t in trades:
        groups[key_fn(t)].append(t)
    return {k: {"trade_count": len(v), "resolved_count": len(_resolved(v)),
                "win_rate": win_rate(v), "total_r": total_r(v), "average_r": average_r(v)}
            for k, v in groups.items()}


def build_view(label: str, trades: list[Trade]) -> PerformanceView:
    resolved = _resolved(trades)
    return PerformanceView(
        label=label, trade_count=len(trades), resolved_count=len(resolved),
        unresolved_count=len(trades) - len(resolved),
        win_rate=win_rate(trades), total_r=total_r(trades), average_r=average_r(trades),
        profit_factor=profit_factor(trades), max_drawdown_r=max_drawdown_r(trades),
        by_pair=_group_stats(trades, lambda t: t.pair),
        by_direction=_group_stats(trades, lambda t: t.direction),
    )


def cost_sensitivity(trades: list[Trade], cost_r_per_trade: list[float]) -> dict:
    """Total/average R after subtracting a flat per-trade cost (in R) from
    every RESOLVED trade, for each assumed cost level. This is a sensitivity
    sweep, not a claim about the real cost -- financing/commission/slippage
    are genuinely unmeasured here (see the caveats already shipped in
    research_gap_evidence.reconstruct(): unaccounted_costs)."""
    r = _resolved(trades)
    out = {}
    for cost in cost_r_per_trade:
        adjusted = [t.r_multiple - cost for t in r]
        out[cost] = {"total_r": sum(adjusted) if adjusted else None,
                    "average_r": (sum(adjusted) / len(adjusted)) if adjusted else None}
    return out


def build_combined_view(live_trades: list[Trade], reconstructed_trades: list[Trade]) -> PerformanceView:
    """View C: explicitly mixed-provenance. The label makes this
    unambiguous in any report; callers must never drop the label when
    presenting this view's numbers alongside A or B."""
    combined = list(live_trades) + list(reconstructed_trades)
    return build_view("C_combined_mixed_provenance_subject_to_reconstruction_limitations", combined)
