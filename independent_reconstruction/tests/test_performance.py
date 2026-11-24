import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import performance as perf


def T(pair="EURUSD", direction="long", r=None, exit_t=None):
    return perf.Trade(pair=pair, direction=direction, r_multiple=r, exit_time_utc=exit_t)


def test_win_rate_only_over_resolved_trades():
    trades = [T(r=1.0), T(r=-1.0), T(r=0.5), T(r=None)]
    assert perf.win_rate(trades) == 2 / 3


def test_win_rate_none_when_nothing_resolved():
    assert perf.win_rate([T(r=None), T(r=None)]) is None


def test_total_and_average_r():
    trades = [T(r=1.0), T(r=-0.5), T(r=0.25)]
    assert perf.total_r(trades) == 0.75
    assert perf.average_r(trades) == 0.25


def test_profit_factor_normal_case():
    trades = [T(r=2.0), T(r=-1.0), T(r=1.0)]
    assert perf.profit_factor(trades) == 3.0  # wins=3.0, losses=1.0


def test_profit_factor_none_when_no_losses():
    assert perf.profit_factor([T(r=1.0), T(r=2.0)]) is None


def test_profit_factor_none_when_nothing_resolved():
    assert perf.profit_factor([T(r=None)]) is None


def test_max_drawdown_r_ordered_by_exit_time():
    trades = [T(r=1.0, exit_t="2026-01-03T00:00:00+00:00"),
              T(r=-2.0, exit_t="2026-01-01T00:00:00+00:00"),
              T(r=0.5, exit_t="2026-01-02T00:00:00+00:00")]
    # chronological order: -2.0 (cum=-2, peak=0, dd=2), 0.5 (cum=-1.5, dd=1.5), 1.0 (cum=-0.5, dd=0.5)
    assert perf.max_drawdown_r(trades) == 2.0


def test_max_drawdown_excludes_trades_without_exit_time_from_the_curve():
    trades = [T(r=-5.0, exit_t=None), T(r=1.0, exit_t="2026-01-01T00:00:00+00:00")]
    assert perf.max_drawdown_r(trades) == 0.0  # the -5.0 never enters the ordered curve


def test_max_drawdown_none_when_nothing_resolved():
    assert perf.max_drawdown_r([T(r=None)]) is None


def test_by_pair_and_by_direction_breakdown():
    trades = [T(pair="EURUSD", direction="long", r=1.0), T(pair="EURUSD", direction="short", r=-1.0),
              T(pair="USDJPY", direction="long", r=0.5)]
    view = perf.build_view("A_test", trades)
    assert view.by_pair["EURUSD"]["trade_count"] == 2
    assert view.by_pair["USDJPY"]["trade_count"] == 1
    assert view.by_direction["long"]["trade_count"] == 2
    assert view.by_direction["short"]["trade_count"] == 1


def test_view_counts_resolved_and_unresolved_correctly():
    trades = [T(r=1.0), T(r=None), T(r=None)]
    view = perf.build_view("A_test", trades)
    assert view.trade_count == 3
    assert view.resolved_count == 1
    assert view.unresolved_count == 2


def test_cost_sensitivity_subtracts_flat_cost_from_resolved_trades_only():
    trades = [T(r=1.0), T(r=-0.5), T(r=None)]
    out = perf.cost_sensitivity(trades, [0.0, 0.1])
    assert out[0.0]["total_r"] == 0.5
    assert out[0.1]["total_r"] == pytest.approx(0.3)  # (1.0-0.1) + (-0.5-0.1)


def test_combined_view_is_explicitly_labelled_mixed_provenance():
    live = [T(r=1.0)]
    reconstructed = [T(r=-0.5)]
    view = perf.build_combined_view(live, reconstructed)
    assert "mixed_provenance" in view.label
    assert view.trade_count == 2
    assert view.total_r == 0.5


def test_combined_view_never_silently_drops_unresolved_trades():
    live = [T(r=None)]
    reconstructed = [T(r=1.0)]
    view = perf.build_combined_view(live, reconstructed)
    assert view.trade_count == 2
    assert view.unresolved_count == 1
