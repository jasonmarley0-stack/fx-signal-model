import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import performance as perf

# View A: original live-observed (4 controls, production truth, independent of this exercise's own validation success)
view_a_trades = [
    perf.Trade(pair="USDCAD", direction="short", r_multiple=-0.7541152263374584, exit_time_utc="2026-10-25T21:04:56.913807+00:00"),
    perf.Trade(pair="USDJPY", direction="long", r_multiple=1.07246376811598, exit_time_utc="2026-11-03T15:17:18.168919+00:00"),
    perf.Trade(pair="EURUSD", direction="short", r_multiple=0.8926488013576076, exit_time_utc="2026-11-11T13:17:01.882877+00:00"),
    perf.Trade(pair="USDJPY", direction="long", r_multiple=0.9556313993174356, exit_time_utc="2026-11-11T13:49:53.282780+00:00"),
]

# View B: independently reconstructed, originally-unresolved trades only (never already in A)
view_b_trades = [
    perf.Trade(pair="USDJPY", direction="long", r_multiple=-1.0000000000000482, exit_time_utc="2026-10-22T03:08:42.529605+00:00"),
    perf.Trade(pair="USDJPY", direction="short", r_multiple=1.0172802037712931, exit_time_utc="2026-10-23T00:05:48.922447+00:00"),
    perf.Trade(pair="GBPUSD", direction="long", r_multiple=-1.0051948051948103, exit_time_utc="2026-11-10T15:29:46.428831+00:00"),
]

# Remaining unresolved (15 trades, genuinely unknown outcome)
unresolved_trades = [
    perf.Trade(pair="EURUSD", direction="short", r_multiple=None),
    perf.Trade(pair="GBPUSD", direction="long", r_multiple=None),
    perf.Trade(pair="USDJPY", direction="short", r_multiple=None),
    perf.Trade(pair="USDCAD", direction="short", r_multiple=None),
    perf.Trade(pair="USDCHF", direction="short", r_multiple=None),
    perf.Trade(pair="EURUSD", direction="long", r_multiple=None),
    perf.Trade(pair="USDJPY", direction="long", r_multiple=None),
    perf.Trade(pair="USDCHF", direction="short", r_multiple=None),
    perf.Trade(pair="USDCAD", direction="short", r_multiple=None),
    perf.Trade(pair="NZDUSD", direction="short", r_multiple=None),
    perf.Trade(pair="NZDUSD", direction="short", r_multiple=None),
    perf.Trade(pair="AUDUSD", direction="short", r_multiple=None),
    perf.Trade(pair="GBPUSD", direction="short", r_multiple=None),
    perf.Trade(pair="NZDUSD", direction="short", r_multiple=None),
    perf.Trade(pair="GBPUSD", direction="short", r_multiple=None),
]


def dump(label, view):
    print(f"\n=== {label} ===")
    print(f"trade_count={view.trade_count} resolved={view.resolved_count} unresolved={view.unresolved_count}")
    print(f"win_rate={view.win_rate} total_r={view.total_r} average_r={view.average_r}")
    print(f"profit_factor={view.profit_factor} max_drawdown_r={view.max_drawdown_r}")
    print("by_pair:", json.dumps(view.by_pair, indent=2))
    print("by_direction:", json.dumps(view.by_direction, indent=2))


view_a = perf.build_view("A_original_live_observed", view_a_trades)
view_b = perf.build_view("B_independently_reconstructed", view_b_trades)
view_c = perf.build_combined_view(view_a_trades + unresolved_trades, view_b_trades)

dump("View A (original live-observed, n=4)", view_a)
dump("View B (independently reconstructed, n=3)", view_b)
dump("View C (combined, mixed-provenance, n=7 determined + 15 unresolved = 22)", view_c)

print("\n=== Cost sensitivity (View C, resolved trades only) ===")
all_resolved = view_a_trades + view_b_trades
sens = perf.cost_sensitivity(all_resolved, [0.0, 0.02, 0.05, 0.10])
print(json.dumps(sens, indent=2))

print("\n=== Expectancy (win_rate * avg_win + loss_rate * avg_loss), View C ===")
wins = [t.r_multiple for t in all_resolved if t.r_multiple > 0]
losses = [t.r_multiple for t in all_resolved if t.r_multiple < 0]
n = len(all_resolved)
avg_win = sum(wins) / len(wins) if wins else 0
avg_loss = sum(losses) / len(losses) if losses else 0
expectancy = (len(wins) / n) * avg_win + (len(losses) / n) * avg_loss
print(f"n={n} wins={len(wins)} losses={len(losses)} avg_win={avg_win:.4f} avg_loss={avg_loss:.4f} expectancy={expectancy:.4f}R/trade")
