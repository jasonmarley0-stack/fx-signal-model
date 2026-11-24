# V2 Performance Recovery Report — STATUS: pending real data

Cannot be completed yet — same reason as `RECONSTRUCTION_VALIDATION_REPORT.md`. The three required
performance views (brief section 8) need real trade outcomes; none exist yet in this environment.

What's actually built and tested, ready to populate the moment the real reconstructed ledger exists:
`performance.py` (`build_view`, `build_combined_view`, `cost_sensitivity`, by-pair/by-direction
breakdowns, win rate, total/average R, profit factor, max realised drawdown ordered by exit time) —
34/34 tests passing against synthetic trades (`tests/test_performance.py`), covering: resolved-only
denominators, the "no losses → profit factor undefined, not infinity" case, drawdown excluding trades
with no exit timestamp from the ordered curve rather than guessing their position, and the combined
view's mandatory mixed-provenance label.

## What will go here once the inputs arrive and the engine has run

**A. Original live-observed performance** — the 4 (or however many) trades the production observer
itself resolved, unchanged from `research_kpi_v2.json`.

**B. Independently reconstructed performance** — every trade this engine resolved
(`reconstructed_targeted`/`reconstructed_stopped`/`reconstructed_time_exit`), excluding the live trades
from view A (so A and B never double-count a trade).

**C. Combined, explicitly labelled mixed-provenance** — A ∪ B, with `performance.py`'s label carried
through to every rendering of this view: `C_combined_mixed_provenance_subject_to_reconstruction_limitations`.
Per the brief, this is never presented as equivalent to a fully live-observed track record.

Each view will report: trade count, win rate, total R, average R, profit factor, max realised
drawdown, by-pair breakdown, long vs. short breakdown, and a transaction-cost sensitivity sweep (0, and
a small set of plausible per-trade cost assumptions in R, clearly marked as a sensitivity analysis, not
a measured cost — financing/commission/slippage remain genuinely unmeasured, per the existing
`unaccounted_costs` caveat already shipped in `research_gap_evidence.py`).

**Remaining unresolved count and impact assessment**: however many of the 16 originally-unresolved
trades this engine still cannot resolve (per its own conservative rules — see
`RECONSTRUCTION_METHODOLOGY.md` section 3.7) will be reported explicitly, along with a discussion of
how the aggregate performance figures above could shift if those remaining unknowns later resolved as
wins, losses, or a realistic mix — not a point estimate presented as fact.
