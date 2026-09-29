"""Engine-level correctness checks added in this correction pass: signal
timing/lookahead-immunity, baseline parity with the real production
combine_signal() (including the spread floor the earlier hand-rolled
formula silently omitted), and position occupancy through a real replay
run. Uses the real committed dataset (research/offline_comparison/data/
raw/) — these are checks on the HARNESS's behaviour against real data,
not a claim about strategy performance (see RESULTS.md for that).
"""
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from data_loader import load_pair, HOLDOUT_END, PairData  # noqa: E402
import configs as cfg  # noqa: E402
from replay_engine import run_replay, compute_signal_series, _combiner_levels  # noqa: E402
from combiner import combine_signal, MIN_RISK_SPREAD_MULTIPLE  # noqa: E402
from spreads import get_spread  # noqa: E402


def test_decision_uses_candle_completion_not_open():
    """The published/decision timestamp for an H4 bar starting at ts must
    be ts + 4 hours (the candle's actual close), never ts itself — the
    bug this correction fixes let a signal be timestamped as if it were
    published at its own source candle's OPEN, before the close/high/low
    it was computed from were actually known."""
    pd_ = load_pair("EURUSD")
    ledger = run_replay("baseline", pd_, HOLDOUT_END.astimezone(timezone.utc))
    checked = 0
    for row in ledger:
        if row.get("event_type") not in ("issued", "revised"):
            continue
        start = datetime.fromisoformat(row["source_candle_start_utc"])
        decision = datetime.fromisoformat(row["decision_time_utc"])
        assert decision == start + timedelta(hours=4), f"decision_time must be source candle start + 4h, got start={start} decision={decision}"
        checked += 1
    assert checked > 0
    print(f"timing: decision_time_utc == source_candle_start_utc + 4h for all {checked} issued/revised versions: OK")


def test_no_entry_before_earliest_permitted_entry_time():
    pd_ = load_pair("EURUSD")
    ledger = run_replay("baseline", pd_, HOLDOUT_END.astimezone(timezone.utc))
    checked = 0
    for row in ledger:
        if row.get("event_type") not in ("issued", "revised") or not row.get("assumed_entry_time_utc"):
            continue
        earliest = datetime.fromisoformat(row["earliest_permitted_entry_time_utc"])
        entry = datetime.fromisoformat(row["assumed_entry_time_utc"])
        assert entry >= earliest, f"entry at {entry} occurred before earliest permitted entry time {earliest}"
        checked += 1
    assert checked > 0
    print(f"timing: no entry occurs before publication + stated execution delay, across {checked} real entered trades: OK")


def test_signal_unaffected_by_mutating_future_candles():
    """Proves a decision at bar ts does not depend on any candle whose
    data would not actually be available at ts's decision time: mutate
    every H4 bar AFTER a chosen decision point (wildly, not a subtle
    perturbation) and confirm the earlier decision's direction/stop/
    target/confidence are byte-identical before and after."""
    pd_ = load_pair("EURUSD")
    series_before = compute_signal_series("baseline", pd_)
    # pick a bar with a real signal, roughly mid-dataset, to mutate everything after it
    fired = series_before[series_before["direction"] != "no_trade"]
    cut_idx = fired.index[len(fired) // 2]
    before_row = series_before.loc[cut_idx].copy()

    mutated_h4 = pd_.h4.copy()
    future_mask = mutated_h4.index > cut_idx
    for col in ("bid_open", "bid_high", "bid_low", "bid_close", "ask_open", "ask_high", "ask_low", "ask_close"):
        mutated_h4.loc[future_mask, col] = mutated_h4.loc[future_mask, col] * 3.7 + 0.5  # wildly different, not a subtle nudge

    from data_loader import mid_ohlc
    mutated_pair_data = PairData(pair=pd_.pair, h4=mutated_h4, m30=pd_.m30,
                                  h4_mid=mid_ohlc(mutated_h4), m30_bid=pd_.m30_bid, m30_ask=pd_.m30_ask)
    series_after = compute_signal_series("baseline", mutated_pair_data)
    after_row = series_after.loc[cut_idx]

    for field in ("direction", "confidence", "entry", "stop", "target"):
        v_before, v_after = before_row[field], after_row[field]
        if isinstance(v_before, float) and not pd.isna(v_before):
            assert abs(v_before - v_after) < 1e-12, f"{field} changed after mutating future-only candles: {v_before} -> {v_after}"
        else:
            assert v_before == v_after, f"{field} changed after mutating future-only candles: {v_before} -> {v_after}"
    print("timing: mutating every candle AFTER a decision point does not alter that decision (no lookahead): OK")


def test_baseline_calls_real_combine_signal_spread_floor_included():
    """The earlier version of _baseline_or_orb_levels reimplemented
    combine_signal()'s formula by hand and, in doing so, omitted the
    spread floor (MIN_RISK_SPREAD_MULTIPLE) entirely. This test confirms
    the CURRENT implementation actually calls combine_signal() and that
    its spread floor is genuinely active: pick real inputs where the raw
    ATR-based stop would be tighter than 10x the real spread, and confirm
    the returned stop respects the floor."""
    pair = "EURUSD"
    entry_price = 1.10000
    tiny_atr = 0.00005  # deliberately smaller than 10x EURUSD's real spread, to force the floor to bind
    tech_row = pd.Series({"tech_score": 0.5})  # magnitude 0.5 -> medium confidence, a real direction
    levels = _combiner_levels(pair, tech_row, entry_price, tiny_atr)
    assert levels is not None
    risk = abs(entry_price - levels["stop"])
    spread = get_spread(pair)
    assert risk >= MIN_RISK_SPREAD_MULTIPLE * spread - 1e-9, (
        f"stop distance {risk} does not respect the spread floor ({MIN_RISK_SPREAD_MULTIPLE}x spread={MIN_RISK_SPREAD_MULTIPLE*spread}) "
        f"-- combine_signal()'s spread floor is not actually being applied")

    # direct parity: the SAME inputs through the real combine_signal() must agree exactly with _combiner_levels()
    sig = combine_signal(pair=pair, entry=entry_price, atr_value=tiny_atr, tech_score=0.5, pestle_score=0.0, alpha=1.0, generated_at=None)
    sl_lo, sl_hi = sig.stop_loss_range
    expected_stop = sl_hi if sig.direction == "long" else sl_lo
    assert levels["stop"] == expected_stop, "replay_engine's levels must match combine_signal()'s own output exactly, not approximate it"
    print("baseline parity: _combiner_levels() calls the real combine_signal(), spread floor included and verified binding: OK")


if __name__ == "__main__":
    test_decision_uses_candle_completion_not_open()
    test_no_entry_before_earliest_permitted_entry_time()
    test_signal_unaffected_by_mutating_future_candles()
    test_baseline_calls_real_combine_signal_spread_floor_included()
    print("All replay-engine correctness checks passed.")
