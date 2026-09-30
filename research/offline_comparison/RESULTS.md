# Offline strategy comparison — results (corrected, deadline-boundary rerun)

Research branch: `research/offline-strategy-comparison`. Reproduce with:

```bash
.venv/bin/python3 research/offline_comparison/run_experiment.py
```

**This is the second correction of the first pass (commit `cc64fef`).** Two superseded outputs are kept for the record — do not draw conclusions from either: `output_superseded_v1/` (the original, pre-correction run) and `output_superseded_v2_pre_deadline_boundary_fix/` (after the first correction pass, before the deadline-boundary fix below). Each has its own `SUPERSEDED.md` stating exactly what was wrong. Every number in this document supersedes both. Same three strategies, frozen parameters, dataset, and chronological split throughout all three passes — no candidate added, no parameter tuned.

## Deadline-boundary correction (this rerun)

A further real defect, found after the first correction pass: the completed-candle stop/target scan still included a candle whose own OPEN was at-or-before the holding deadline but whose FULL SPAN (open + interval) extended past it — letting that candle's high/low, which partly reflects price action *after* the deadline, resolve a stop/target/ambiguity decision that should have been bounded strictly by the deadline. This is distinct from (and survived) the entry-candle-inclusion fix in the first correction pass.

Fixed in `replay_scorer.py`: only fully-completed candles (`open + interval <= deadline`) may contribute a stop/target/ambiguity hit via their high/low. At the boundary itself, only the single candle whose own open lands **exactly** on the deadline — this system's entries are always confirmed on a grid-aligned M30 open, and `max_holding_time_hours` is always a whole multiple of the M30 interval, so such a candle exists whenever the data isn't gapped there — may contribute, and only via its own open (a gap-through check, or the time-exit price itself). Without an exact-deadline quote, the outcome is `incomplete_coverage`; an earlier, stale open is never fabricated into a deadline fill. Five dedicated tests added (`test_deadline_boundary.py`): both directions, a gap-through at the exact deadline quote for stop and for target, both levels breached simultaneously at that one quote (ambiguous), a genuine data gap with no exact-deadline quote (incomplete coverage), and confirmation that an ordinary stop/target hit on a fully-completed candle well before the deadline still resolves normally.

**Effect on results: small and consistent with the fix's scope** — it only changes trades whose resolution depended on the last, boundary-straddling candle, a minority of completed trades. Full-window avg R per completed trade: baseline +0.0866 → **+0.0859**; genuine_orb +0.0072 → **+0.0083**; trend_pullback −0.0620 → **−0.0613**. No conclusion changes.

**Every alert in this report is a replay — hypothetical, scored against historical OANDA candles. No historical subscriber received any of these alerts.** The holdout period (2026-07-01 → 2026-09-27) was already inspected once, in the superseded pass — it is a **fixed evaluation period, not fresh untouched validation**, and is described that way throughout this document.

## What changed, and why it matters

| # | Defect in the first pass | Fix | Effect on results |
|---|---|---|---|
| 1 | A signal was timestamped as published at its source H4 candle's **open**, using close/high/low only actually known 4 hours later at that candle's **completion**. Entry-window checking could therefore begin before the deciding candle had even closed. | `decision_time = source_candle_start + 4h`, computed directly (never inferred from "the next available row," which would be wrong across a data gap). Recorded as four separate fields per alert: `source_candle_start_utc`, `source_candle_completion_utc`, `decision_time_utc`, `earliest_permitted_entry_time_utc`. | **Large.** `insufficient_data_entry` (price touched the entry range but no candle's open ever confirmed a fill) collapsed from ~30–40% of alerts to essentially zero across all three candidates — most of what looked like an M30-resolution data-quality limit in the first pass was actually this timing bug: checking started 4 hours "too early" relative to the entry level. |
| 2 | `replay_scorer.py` excluded the entry candle itself from the stop/target scan (`index > entry_time`), reintroducing a bug already fixed once in production `src/alert_scorer.py`. | Changed to `>=`. | A same-candle stop/target/ambiguity is now attributed correctly instead of possibly being overridden by a later candle's clean result. |
| 3 | A time exit was priced off a qualifying candle's **close**, which can represent an instant *after* the stated holding deadline (candle timestamps denote opens). | Priced off that candle's **open** instead — the latest price actually known at or before the deadline. | Time-exit prices shifted, generally slightly, toward the more conservative (earlier-known) value. |
| 4 | No gap handling: a stop/target reached via a candle that had already gapped through it at its own open was priced as an exact fill at the stated level, which was never actually quoted. | If the candle's own open has already crossed the level, price the exit at that open; only price exactly at the level when it was reached via the candle's high/low without the open already being past it. | Gapped exits now price worse (more conservatively) than before. |
| 5 | `avg_net_r_per_entered_trade` (headline "+0.094" in the first pass) was actually averaged over **completed trades** (130), not all entered trades (160). `avg_net_r_per_eligible_alert` ("+0.086") silently **excluded every unknown-outcome alert** from its denominator (142 known-outcome cases, not 244 eligible alerts) despite its name. | Every averaged metric now states its own denominator in its name: `avg_net_r_per_completed_trade`, `avg_net_r_per_known_outcome_incl_missed_at_zero`, and `avg_net_r_per_all_eligible_alert` (reported as **undetermined**, not silently computed, whenever any eligible alert's outcome is unknown — which is every candidate, every period, in this dataset). Added `target_hit_rate_of_completed` (win = target hit) as a metric separate from `profitable_trade_rate_of_completed` (win = any positive R, including a profitable time exit). | No result changed here — the correction is entirely in what the numbers are honestly labelled as covering. |
| 6 | The baseline candidate **reimplemented** `combine_signal()`'s confidence/stop/target formula by hand rather than calling the real function, and in doing so **omitted the spread floor** (`MIN_RISK_SPREAD_MULTIPLE`) entirely — despite the first pass's `RESULTS.md` stating baseline "imports combine_signal() directly." | `_combiner_levels()` now calls the real, frozen `src/combiner.py::combine_signal()` for both baseline and genuine_orb, with `alpha=1.0`/`pestle_score=0.0` matching `live_scanner.py`'s actual deployed configuration exactly. Verified with a direct parity test against `combine_signal()`'s own output, and a test confirming the spread floor actually binds when ATR alone would produce a tighter stop. | Stop distances (and therefore risk, and therefore R) changed for both baseline and genuine_orb wherever the spread floor was previously (wrongly) not applied. |
| 7 | An entered position that resolved to `ambiguous_intrabar_exit` or `incomplete_coverage` has no `exit_time_utc` — the old occupancy check only recognised `state == "open"` or a present `exit_time_utc`, so it silently **freed the pair for a new entry** despite a real, unresolved position outstanding. | Any entered position without a clean `exit_time_utc` now conservatively reserves the pair through `entry_time + max_holding_time_hours` (the full stated holding window). Flagged per-row via `conservative_occupancy=True` on the suppressed rows it produces. | Fewer new entries are issued while an ambiguous/incomplete position is conservatively held — `issued_versions` dropped moderately for every candidate. |

Item 7 explicitly trades off opportunity count against occupancy correctness: `issued_versions` (full window) went from 244→224 (baseline), 1,160→919 (genuine_orb), 667→635 (trend_pullback) — fewer opportunities counted, because more of them are now correctly recognised as blocked by a still-outstanding, unresolved position rather than incorrectly treated as free.

Nothing about the three candidates' own rules changed — same baseline formula (now correctly invoked), same genuine-M30-opening-range logic, same fixed trend-pullback rule set, same dataset, same chronological split, same warm-up.

## Production rules vs. common research execution policy — stated prominently, as required

**Frozen from production, unmodified, called directly (not reimplemented):** `src/strategies/composite.py`'s technical scoring and `src/combiner.py`'s `combine_signal()` — confidence thresholds, reward:risk tiers, ATR-based SL/TP formula, and spread floor. This governs baseline and genuine_orb's levels; trend_pullback uses its own fixed, documented rule set (`research/offline_comparison/strategies/trend_pullback.py`) instead.

**Common research execution policy — NOT part of production, identical across all three candidates so any difference between them is attributable to the entry rule, not the policy:**
- **4-hour entry validity window** (`ENTRY_VALIDITY_HOURS=4.0`) — production's actual entry-validity default is 90 minutes (`alert_lifecycle.py`'s `DEFAULT_ENTRY_VALIDITY_MINUTES`); this research contract uses a longer, separately-chosen window, unchanged from the first pass.
- **H4-candle-completion decision cadence with zero additional execution delay** (`EXECUTION_DELAY_MINUTES=0`) — production polls every 30 minutes via `live-scanner.timer` and acts on the latest closed H4 candle; this replay's cadence is comparable in spirit but is a research-contract choice, not a copy of the live timer's exact behaviour.
- **One-open-position-per-pair suppression policy** — production has no notion of position tracking at all today; this is entirely a research-harness construct for this milestone's opportunity/R accounting, not a production rule.
- **Conservative occupancy for ambiguous/incomplete outcomes** (item 7 above) — a research-only accounting choice.

None of these four were changed in this correction pass — they are restated here, prominently, because the correction order specifically required distinguishing them from the frozen production rules, not because their values moved.

## Headline comparison (full window: dev + holdout, 2025-11-15 → 2026-09-27)

| Metric | Baseline | Genuine ORB | Trend pullback |
|---|---:|---:|---:|
| Issued versions | 224 | 919 | 635 |
| Eligible alerts | 224 | 919 | 635 |
| Assumed entries | 218 | 907 | 614 |
| **Completed trades** | 166 | 706 | 540 |
| **Unknown outcomes** (entry+exit) | 53 (23.7% of eligible) | 202 (22.0%) | 75 (11.8%) |
| — unknown-entry (insufficient data) | 1 | 1 | 1 |
| — unknown-exit (ambiguous/incomplete/open) | 52 | 201 | 74 |
| Confirmed missed entries (0 P&L) | 5 | 11 | 20 |
| Target-hit rate (of completed) | 16.3% | 14.7% | **51.7%** |
| **Profitable-trade rate** (of completed, target OR profitable time exit) | 59.6% | 51.4% | 54.1% |
| **Avg net R / completed trade** (n shown) | **+0.0859** (166) | +0.0083 (706) | **−0.0613** (540) |
| Avg net R / known outcome, incl. missed at 0 (n shown) | +0.0834 (171) | +0.0082 (717) | −0.0591 (560) |
| Avg net R / ALL eligible alerts | **undetermined** — 53 unknown outcomes | **undetermined** — 202 unknown | **undetermined** — 75 unknown |
| Max drawdown, R, **completed trades only (partial)** | −3.74 | −28.57 | −46.91 |
| Unknown-outcome scenario: if all unknown-exit = this candidate's best observed R | +0.312 | +0.301 | +0.039 |
| Unknown-outcome scenario: if all unknown-exit = this candidate's worst observed R | −0.173 | −0.215 | −0.187 |

**Trend pullback still has by far the highest target-hit rate (51.7% vs. 16.3%/14.7%) and is still the worst performer on average R (−0.062) — the same qualitative lesson as the first pass survives correction: a target-hit/win-rate framing alone would rank trend_pullback first; average R ranks it last.** The *profitable-trade rate* (which also counts a profitable time exit as a win) narrows that gap somewhat (59.6% / 51.4% / 54.1% — much closer together) — itself informative: baseline and genuine_orb rely much more heavily on profitable time exits than clean target hits to produce a positive-R trade, which trend_pullback does not.

**Unknown-outcome scenario figures are explicitly a labelled sensitivity scenario, not a guaranteed bound** — an unresolved trade is not mathematically constrained to fall within the range of outcomes this candidate has actually observed; a real unresolved case could in principle do better or worse than either extreme shown.

**Every "drawdown" figure above is R drawdown computed on completed trades only — labelled PARTIAL wherever unknown outcomes remain excluded (every row above) — and is not, and must not be read as, an account-percentage drawdown.** No account-percentage drawdown is computed anywhere in this milestone (no position sizing or account model is defined).

## Dev vs. holdout — the previously-inspected holdout is a fixed period, not fresh validation

| Candidate | Dev avg R/completed | Holdout avg R/completed | Dev max DD (partial) | Holdout max DD (partial) |
|---|---:|---:|---:|---:|
| Baseline | +0.0899 (n=117) | +0.0764 (n=49) | −3.74 | −2.38 |
| Genuine ORB | +0.0244 (n=502) | **−0.0313** (n=204) | −16.22 | −16.03 |
| Trend pullback | −0.0741 (n=390) | −0.0279 (n=150) | −43.83 | −15.53 |

Baseline is again the only candidate positive in both periods, now on a larger completed-trade sample (117 dev / 49 holdout, vs. 94/36 in the first pass) and with a materially smaller drawdown (−3.74R full-window vs. −6.83R in the first pass, now correctly reflecting the spread floor). Genuine ORB's holdout average R flipped from a barely-positive +0.002 (first pass) to a clearly **negative** −0.031 after correction — the same "does not survive the holdout" conclusion holds, now more decisively. Trend pullback is negative in both periods post-correction (it was slightly positive in holdout, on the buggy pre-correction numbers) — the negative conclusion is now consistent across both periods rather than mixed.

**This holdout period was already examined in the superseded first pass.** Re-running the corrected harness against the same fixed 2026-07-01→2026-09-27 window is a legitimate bug-fix rerun, not a second independent look at fresh data — stated here explicitly so it is never described as untouched out-of-sample validation in any later summary of this milestone.

## By pair and by month

`output/by_pair_*.csv` and `output/by_month_*.csv`, regenerated (all months in the window included, zero-filled where nothing fired — none actually occurred).

## Sensitivity to cost and execution delay (per completed trade)

| Candidate | Real bid/ask | Zero-cost mid | Cost impact | +30min delay | Delay impact |
|---|---:|---:|---:|---:|---:|
| Baseline | +0.0859 | +0.1165 | **−0.0306** | +0.0650 | −0.0208 |
| Genuine ORB | +0.0083 | +0.0278 | **−0.0195** | −0.0544 | −0.0628 |
| Trend pullback | −0.0613 | −0.0167 | **−0.0446** | −0.0433 | +0.0180 |

Real spread cost is smaller in absolute terms than in the first pass (the spread floor now correctly widening stops reduces the *relative* cost drag per unit of risk) but remains real, material, and correctly signed (always negative) for every candidate.

## Recommendation (plain English) — updated

- **Baseline: further offline investigation, then prospective observation with timestamped bid/ask.** Positive in both dev and holdout, now on the largest completed-trade sample of the three relative to its opportunity count, with the smallest drawdown and correctly reflecting the production spread floor. Still not enough evidence to skip straight to execution — the unknown-outcome scenario band (+0.31 to −0.17) is still wide relative to the point estimate — but this is the strongest candidate of the three on this evidence and is worth carrying into Stage 2.
- **Genuine ORB: reject as tested, or at most further offline investigation only.** The correction made the holdout result unambiguously negative (−0.031 avg R/completed trade, was a barely-positive +0.002 pre-correction) while dev remained positive — the dev-only strength is now even more clearly not something the holdout confirms. Repairing ORB still changes signal *volume* enormously (224→919 issued vs. baseline) without evidence of a quality improvement.
- **Trend pullback: reject as tested.** Negative average R in the full window, both sub-periods, and against every metric except the target-hit and profitable-trade rates — which is exactly the pattern that makes win-rate-only ranking misleading. Highest issue-volume conviction (51.7% target-hit rate) paired with the worst R and by far the worst drawdown (−46.91R) of the three.

## Roadmap

| Stage | Status | Evidence | Next action |
|---|---|---|---|
| 1. Offline comparison | **Done, corrected (2 correction passes)** | This document, `output/ledger_full.csv` (10,484 rows), `output/summary_by_candidate_period.csv`, 3 equity/drawdown plots labelled PARTIAL, 22 harness-correctness checks (`test_replay_correctness.py` + `test_replay_engine_correctness.py` + `test_deadline_boundary.py`, all passing), 5 pre-existing lifecycle test files still passing unmodified | None required to close this stage |
| 2. Prospective observation with timestamped bid/ask | Not started | — | Baseline is the only candidate with evidence supporting continued observation on this pass; point a shadow process at it, scored with `src/alert_lifecycle.py`/`src/alert_scorer.py`, collecting real bid/ask at alert time |
| 3. Bounded OANDA practice execution | Not started | — | Only after stage 2 |
| 4. Product readiness | Not started | — | A decision point, not an engineering task |

Engineering progress on this milestone (including this correction pass) is complete. Strategy performance is separately and explicitly: baseline warrants continued offline/prospective investigation; genuine ORB and trend pullback do not, as tested.
