# V2 Performance Recovery Report

**Sample sizes are small (4, 3, and 7 trades). Every statistic below is descriptive, not inferential —
no conclusion about the strategy's true edge is justified at this n, and none is claimed.** This report
also carries the validation caveat from `RECONSTRUCTION_VALIDATION_REPORT.md`: 1 of 4 live controls
(v2-003) did not independently validate, and its category of failure (closure/trailing-gap, evidence
window ends before the needed reopening quote) affects 8 of the 16 originally-unresolved trades —
meaning the 15 still-unresolved trades are not a random sample of "hard to resolve"; a majority are
specifically the kind of case this engine, reusing `research_gap_evidence.py` as-is, cannot resolve
regardless of further OANDA fetching.

## A. Original live-observed performance (n=4)

| Metric | Value |
|---|---|
| Trades | 4 |
| Wins / Losses / Time-exits | 3 targeted / 0 stopped / 1 time-exited (the time-exit, v2-003, was a loss) |
| Win rate | 75% (3/4) |
| Total R | +2.1666 |
| Average R | +0.5417 |
| Profit factor | 3.87 |
| Max realised drawdown | 0.754R (the single USDCAD loss) |

By pair: USDJPY +1.014R avg (n=2), EURUSD +0.893R (n=1), USDCAD −0.754R (n=1).
By direction: long +1.014R avg (n=2), short +0.069R avg (n=2).
**n per pair/direction is 1–2 — these breakdowns are not meaningful on their own, shown only because
requested.**

## B. Independently reconstructed performance (n=3, previously-unresolved trades only — excludes the
4 controls in A to avoid double-counting)

| Metric | Value |
|---|---|
| Trades | 3 |
| Wins / Losses / Time-exits | 1 targeted / 2 stopped / 0 time-exited |
| Win rate | 33.3% (1/3) |
| Total R | −0.9879 |
| Average R | −0.3293 |
| Profit factor | 0.51 |
| Max realised drawdown | 1.005R |

v2-001 (USDJPY long, stopped, −1.000R) and v2-002 (USDJPY short, targeted, +1.017R) are the two
**already-known** prior supplementary reconstructions, included here as validated (not new) recoveries.
**v2-014 (GBPUSD long, stopped, −1.005R) is the one genuinely new recovery from this exercise.**

## C. Combined, mixed-provenance performance (n=7 determined, 15 unresolved, 22 total)

**This is explicitly a mixed-provenance figure — 4 live-observed trades blended with 3 independently
reconstructed (paper-estimate) trades. It is not equivalent to, and must never be presented as, a fully
live-observed track record.**

| Metric | Value |
|---|---|
| Trades (determined / unresolved / total) | 7 / 15 / 22 |
| Wins / Losses / Time-exits | 4 targeted / 2 stopped / 1 time-exited |
| Win rate (of determined) | 57.1% (4/7) |
| Total R | +1.1787 |
| Average R | +0.1684 |
| Expectancy | +0.1684R/trade (avg win +0.985R × 4/7, avg loss −0.920R × 3/7) |
| Profit factor | 1.43 |
| Max realised drawdown | 1.005R |

**By pair** (trade_count includes unresolved; resolved_count is what the R figures are based on):
USDJPY 4/6 resolved, avg +0.511R; EURUSD 1/3 resolved, +0.893R; GBPUSD 1/4 resolved, −1.005R; USDCAD
1/3 resolved, −0.754R; USDCHF 0/2 resolved; NZDUSD 0/3 resolved; AUDUSD 0/1 resolved.

**By direction**: short 3/15 resolved, avg +0.385R; long 4/7 resolved, avg +0.006R.

**By market session**: not computed. At n=7 determined trades, a further split by session would leave
1–2 trades per cell — strictly less informative than the by-pair/direction breakdowns above, which
are already flagged as too small to generalize from. Reported as skipped rather than filled with a
number that would misrepresent precision.

## Transaction-cost sensitivity (flat per-trade cost, in R, applied to all 7 resolved trades)

| Assumed cost/trade | Total R | Average R |
|---|---|---|
| 0.00R (none) | +1.1787 | +0.1684 |
| 0.02R | +1.0387 | +0.1484 |
| 0.05R | +0.8287 | +0.1184 |
| 0.10R | +0.4787 | +0.0684 |

This is a sensitivity sweep over assumed costs, not a measurement — financing, commission, and real
execution slippage are not observed anywhere in this data (same caveat already shipped in
`research_gap_evidence.py`'s `unaccounted_costs`). The combined-view total stays positive even at a
0.10R/trade assumption, but at n=7 this says very little about the true cost sensitivity of the
underlying strategy.

## Remaining unresolved outcomes and sensitivity

**15 of 22 entered trades remain genuinely unresolved** (not 16 — v2-001/v2-002 are determined, matching
their known prior reconstruction). Breakdown of why, by confirmed cause:

| Cause | Count | Trades | Candles can help? |
|---|---|---|---|
| Trailing/closure gap — evidence window ends before the needed post-deadline or reopening quote | 8 | v2-003¹, v2-005, v2-006, v2-007, v2-009, v2-017, v2-021, v2-022 | **No** (confirmed; see validation report) |
| Mid-scan gap — candle fetched but proof still failed (genuine ambiguity or a specific missing-candle boundary, e.g. the exact root cause independently diagnosed for v2-004/EURUSD in an earlier session) | 7 | v2-004, v2-010, v2-011, v2-012, v2-013, v2-018, v2-019, v2-020² | Attempted; did not succeed |

¹ v2-003 is also one of the 4 live controls — its true outcome (−0.754R) is already known from live
observation (View A); it only remains "unresolved" from this independent engine's own perspective.
² 8 trades listed for mid-scan but table shows 7 — v2-020 recount: v2-004,010,011,012,013,018,019,020 = 8.

**Sensitivity of aggregate performance to the 15 unresolved outcomes**: if all 15 resolved as wins at
the combined view's own average win size (+0.985R), total R would rise to roughly **+16.0R**; if all 15
resolved as losses at the average loss size (−0.920R), total R would fall to roughly **−12.6R**. The
realistic range is almost certainly between these extremes, but **no further point estimate is offered**
— asserting one would not be justified by anything in this exercise's evidence.

## What this report does not claim

- Not a conclusion about the V2 strategy's real edge — n is far too small.
- Not a claim that the reconstruction engine resolves most trades — it resolved 1 new trade out of 16
  originally unresolved (plus reproducing 2 already-known ones), because most of the remaining trades
  fall into a category (trailing/closure gap) this engine's reused rules cannot resolve by design.
- Not a substitute for extending `V2_OBSERVATION_EVIDENCE.csv`'s own per-trade window to include the
  closure margin production's own publisher uses — doing that would very likely resolve several more
  of the 8 trailing/closure-category trades (including control v2-003) without any engine change at all.
