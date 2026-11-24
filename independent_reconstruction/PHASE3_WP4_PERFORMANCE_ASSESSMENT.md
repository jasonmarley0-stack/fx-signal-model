# Phase 3 — WP4 Final Performance Assessment

Source: `output/phase2b_reconstruction_results_v4.json` (the Phase 2B run against the extended evidence,
with both engine bugs fixed — see `PHASE3_BASELINE_RECONCILIATION.md`, WP1). 22 entered trades total.

**This is a small-sample retrospective reconstruction, not a live trading record and not a statistically
validated edge.** No win rate, R multiple, or profit factor below should be read as evidence the
underlying strategy works. Costs (financing, commission, slippage) are explicitly unaccounted for in every
figure (see each result's own `unaccounted_costs` field) except where a flat per-trade cost sensitivity is
applied below.

## View A — Original live-completed outcomes (n=4)

The 4 trades the production V2 observer itself resolved live: v2-003, v2-008, v2-015, v2-016.

| Metric | Value |
|---|---|
| Trade count | 4 |
| Wins / Losses / Time-exits | 3 / 1 / 1 (v2-003 is both the loss and the time-exit) |
| Win rate | 75.0% |
| Total R | +2.1666 |
| Expectancy | +0.5417R/trade |
| Profit factor | 3.87 |
| Max realised drawdown | 0.7541R |

By pair: USDJPY (2, +2.0281R), EURUSD (1, +0.8926R), USDCAD (1, -0.7541R).
Long vs short: long n=2 R=+2.0281, short n=2 R=+0.1385.

Chronological: v2-003 (-0.7541R, cum -0.7541) → v2-008 (+1.0725R, cum +0.3183) → v2-015 (+0.8926R, cum
+1.2110) → v2-016 (+0.9556R, cum +2.1666).

## View B — Validated reconstructions (contract-compatible recovery only, n=6)

Trades resolved only through this reconstruction exercise, using original V2 evidence plus
contract-compatible candle-envelope recovery (never V1): v2-001, v2-002, v2-009, v2-014, v2-017, v2-021.

| Metric | Value |
|---|---|
| Trade count | 6 |
| Wins / Losses / Time-exits | 4 / 2 / 3 |
| Win rate | 66.7% |
| Total R | -0.6659 |
| Expectancy | -0.1110R/trade |
| Profit factor | 0.67 |
| Max realised drawdown | 1.0052R |

By pair: USDJPY (2, +0.0173R), NZDUSD (2, +0.2252R), USDCHF (1, +0.0967R), GBPUSD (1, -1.0052R).
Long vs short: long n=2 R=-2.0052, short n=4 R=+1.3393.

Chronological: v2-001 (-1.0000R) → v2-002 (+1.0173R) → v2-009 (+0.0967R) → v2-014 (-1.0052R) → v2-017
(+0.2221R) → v2-021 (+0.0032R).

## View C — Combined mixed-provenance outcomes (n=10)

View A + View B, in chronological order.

| Metric | Value |
|---|---|
| Trade count | 10 |
| Wins / Losses / Time-exits | 7 / 3 / 4 |
| Win rate | 70.0% |
| Total R | +1.5007 |
| Expectancy | +0.1501R/trade |
| Profit factor | 1.54 |
| Max realised drawdown | 1.0052R (at v2-014, 2026-11-10) |

By pair: USDJPY (4, +2.0454R), NZDUSD (2, +0.2252R), EURUSD (1, +0.8926R), USDCHF (1, +0.0967R), USDCAD
(1, -0.7541R), GBPUSD (1, -1.0052R).
Long vs short: long n=4 R=+0.0229, short n=6 R=+1.4778.

Chronological equity curve (cumulative R): v2-001 -1.0000 → v2-002 +0.0173 → v2-003 -0.7368 → v2-008
+0.3356 → v2-009 +0.4324 → v2-014 -0.5728 → v2-015 +0.3198 → v2-016 +1.2755 → v2-017 +1.4975 → v2-021
+1.5007.

### Transaction-cost sensitivity (0 to 0.10R/trade flat)

| Cost/trade | View A total R | View B total R | View C total R |
|---|---|---|---|
| 0.00R | +2.1666 | -0.6659 | +1.5007 |
| 0.02R | +2.0866 | -0.7859 | +1.3007 |
| 0.05R | +1.9666 | -0.9659 | +1.0007 |
| 0.10R | +1.7666 | -1.2659 | +0.5007 |

View C stays positive across the full 0–0.10R range tested, but the margin shrinks from +1.50R to +0.50R
— i.e. a plausible realistic cost assumption alone could erase up to a third of the measured edge.

### Realised vs. unmeasured intratrade drawdown

The "max realised drawdown" above (1.0052R) is the worst peak-to-trough decline in the **sequence of final
R outcomes**, in the order trades closed. It is **not** a measure of intratrade (open-position) drawdown:
these are point-sample reconstructions (quotes and S5 candles checked for stop/target envelope breach),
not continuous tick-level equity tracking. A trade that ultimately timed out or targeted could have moved
substantially further against the position intratrade before recovering — that intratrade excursion is
genuinely unmeasured here and is not claimed to be zero or small.

## View D — V1-assisted exploratory outcomes

**Not applicable — empty.** WP3's V1 investigation did not change any trade's classification. v2-020's 49
V1-corroborated gaps are same-provider corroboration of a weekend market closure, not independent V2
evidence (see `PHASE3_BASELINE_RECONCILIATION.md`, WP3), and per the authorization are not merged into any
result. No trade moved from unresolved to resolved via V1 assistance. View D therefore has zero trades and
is reported as empty, not omitted.

## Remaining unresolved (n=12)

v2-004, v2-005, v2-006, v2-007, v2-010, v2-011, v2-012, v2-013, v2-018, v2-019, v2-020, v2-022 — 12 of the
22 entered trades have no determinate outcome under the original V2 contract, even after the full Phase 2B
evidence extension and the engine fixes in this phase. See WP2/WP3 in `PHASE3_BASELINE_RECONCILIATION.md`
for why each specific one remains unresolved (insufficient candle/quote evidence to rule out a stop/target
touch, or — v2-022 only — a confirmed, unrecoverable 6,060.559s discontinuity).

## Sensitivity analysis: how the 12 unknowns could move the final number

**This is a bounding exercise, not a forecast or a claim about likely outcomes.** Applying the two
extremes observed in the resolved sample (full stop at -1.00R, or the best realised win of
+1.07R) to all 12 unresolved trades, on top of View C's known +1.5007R over 10 trades:

| Scenario | 12 unknowns @ | Resulting 22-trade total R |
|---|---|---|
| All unknowns stop out | -1.00R each | +1.5007 − 12.00 = **-10.4993R** |
| All unknowns hit target | +1.07R each | +1.5007 + 12.84 = **+14.34R** |
| All unknowns breakeven | 0R each | **+1.5007** (unchanged) |

The true range is this wide only because more than half the entered trades (12 of 22) are still
undetermined. **No conclusion about the strategy's edge can be drawn from the determinate subset alone**
— the resolved 10 trades are not a random sample of the 22; they are specifically the ones evidence
happened to be sufficient for, which is itself a potential source of bias this reconstruction cannot
correct for.