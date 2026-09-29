# Offline strategy comparison — results

Research branch: `research/offline-strategy-comparison`, cut from `alert-lifecycle-prospective-scoring` at commit `ef5d480`. Reproduce with:

```bash
.venv/bin/python3 research/offline_comparison/run_experiment.py
```

Outputs land in `research/offline_comparison/output/`: `ledger_full.csv` (every hypothetical alert version and its scored outcome, 8,191 rows), `summary_by_candidate_period.csv`, `by_month_*.csv`, `by_pair_*.csv`, `sensitivity.csv`, `equity_*.png`.

**Every alert in this report is a replay — hypothetical, scored against historical OANDA candles. No historical subscriber received any of these alerts. Nothing here is a live signal, a placed order, or a change to the public performance display.**

## Provenance

- Data: real bid/ask H4 and M30 candles, fetched 2026-09-29 via the existing, authorised OANDA connection (`InstrumentsCandles`, `price=BA`, GET requests only — no order placed). Fetched **on the droplet** (`fetch_research_data.py`, run over SSH — the OANDA credentials live there, not on this machine) and downloaded into `research/offline_comparison/data/raw/` (committed, 8.7 MB, 14 CSV files, one manifest). 7 majors × {H4: 1,537 bars, M30: 12,288 bars} each, 2025-09-30 through 2026-09-27, no gaps.
- OANDA's bid/ask candle endpoint was inspected first, as required, before falling back to anything else — it worked, so no midpoint-only fallback or cost-scenario labelling was needed; every result below uses real observed bid/ask, not a static spread snapshot or a synthetic assumption.
- Dev/holdout split and warm-up were fixed in `data_loader.py` **before** any candidate was run (see git history — `configs.py`/`data_loader.py` predate `output/` in this branch's commits): warm-up 2025-10-01→2025-11-15 (~45 days), dev 2025-11-15→2026-06-30, holdout 2026-07-01→2026-09-27.
- Frozen configurations, entry/exit/cost/position/ambiguity rules: `configs.py`. Every element of the evaluation contract is identical across all three candidates except the entry rule itself (see its docstring for the full list).

## The three candidates

| | Baseline | Genuine ORB | Trend pullback |
|---|---|---|---|
| What it is | Current H4 config, frozen to commit `ef5d480`, unmodified | Baseline's exact formula, with the always-zero H4-relabelled ORB replaced by a genuinely-measured M30 opening range | A structurally different, simple, fixed rule: EMA20/50 trend filter + pullback-to-EMA20-then-resume |
| Code | `src/strategies/composite.py` + `src/combiner.py`, imported directly, not copied | `research/offline_comparison/strategies/genuine_orb.py` | `research/offline_comparison/strategies/trend_pullback.py` |
| Confidence tiers | medium/high/strong-agreement (0.35/0.6/0.75), rr 1.5/2.0 | same | none — single fixed "medium", rr 1.0 |

## Headline comparison (full window: dev + holdout, 2025-11-15 → 2026-09-27)

| Metric | Baseline | Genuine ORB | Trend pullback |
|---|---:|---:|---:|
| Issued versions | 244 | 1,160 | 667 |
| Unique trade opportunities | 244 | 1,160 | 667 |
| Eligible alerts | 244 | 1,160 | 667 |
| Assumed entries | 160 | 837 | 455 |
| Completed trades (resolved) | 130 | 691 | 388 |
| **Opportunities/month** (7 pairs combined) | ~22.2 | ~105.5 | ~60.6 |
| Win rate (of resolved) | 42.3% | 34.3% | **50.0%** |
| Avg net R / entered trade | **+0.094** | +0.046 | **−0.044** |
| Avg net R / eligible alert | +0.086 | +0.044 | −0.042 |
| Sum net R (resolved) | +12.24 | +31.71 | −17.12 |
| Max drawdown (R) | −6.83 | **−19.18** | **−34.99** |
| Missed entries (expired, no fill) | 12 (4.9%) | 24 (2.1%) | 19 (2.8%) |
| Unknown/ambiguous/incomplete | 102 (41.8%) | 445 (38.4%) | 260 (39.0%) |
| Suppressed (existing position) | 552 | 3,330 | 1,488 |

**Trend pullback has the highest win rate of the three (50.0%) and the worst average R (−0.044) and the worst drawdown (−34.99R) — the clearest, most concrete illustration in this dataset of why win rate alone must never be used to rank a candidate.**

## Dev vs. holdout (the discipline that matters most here)

| Candidate | Dev avg R/entered | Holdout avg R/entered | Dev sum R | Holdout sum R | Dev max DD | Holdout max DD |
|---|---:|---:|---:|---:|---:|---:|
| Baseline | +0.063 | +0.176 | +5.92 | +6.32 | −6.04 | −3.00 |
| Genuine ORB | +0.064 | **+0.002** | +31.33 | **+0.38** | −16.67 | −15.15 |
| Trend pullback | **−0.074** | +0.031 | −20.55 | +3.42 | −30.02 | −9.82 |

Genuine ORB's dev-period equity curve (`output/equity_genuine_orb.png`) climbs to +40R and looks strong in isolation — but the holdout period is essentially flat (+0.38R over 205 resolved trades, average R per trade rounding to +0.002), while carrying its own −15.15R drawdown. **This is exactly the pattern the chronological split exists to catch: a dev-period result that does not carry through to the out-of-sample period is a reason to keep testing, not a validated edge.** Trend pullback shows the opposite (weak/negative in dev, positive in holdout) — also not a basis for confidence, just noise in the other direction. Baseline is the only candidate whose dev and holdout results point the same direction (both positive), though the holdout sample (46 entries, 36 resolved) is small enough that this should not be overweighted either.

## Ambiguous, missing-data, and unresolved outcomes — reported, not hidden or zeroed

Roughly **2 in 5 eligible alerts** end in a state that is neither a clean win, loss, nor time-exit, across all three candidates (38–42%). Breakdown of that "unknown/ambiguous" bucket (full window):

| State | Meaning | Baseline | Genuine ORB | Trend pullback |
|---|---|---:|---:|---:|
| `insufficient_data_entry` | price touched the entry range but no M30 bar's own open confirmed a fill — no price is fabricated | most common | most common | most common |
| `incomplete_coverage` | the 30-hour holding boundary landed in a data gap (overwhelmingly weekends — FX markets are closed Fri evening–Sun evening, and a 30h hold from a Thu/Fri entry routinely lands there) | present | present | present | 
| `ambiguous_intrabar_exit` | one M30 bar's bid (long) or ask (short) exit-side range crossed both stop and target — order not determinable from OHLC | present, rare | present, rare | present, rare |

Full per-candidate counts are in `output/summary_by_candidate_period.csv`'s `unknown_or_ambiguous` column, and every individual row's exact state is in `output/ledger_full.csv`.

**Unknown-outcome bounds** (if every currently-unknown *entered* case had instead been this candidate's own best- or worst-case observed R):

| Candidate | Observed avg R | If all unknowns = best observed R | If all unknowns = worst observed R |
|---|---:|---:|---:|
| Baseline | +0.094 | +0.291 | −0.111 |
| Genuine ORB | +0.046 | +0.299 | −0.137 |
| Trend pullback | −0.044 | +0.078 | −0.185 |

Every candidate's sign is not fixed once unknowns are given the benefit or detriment of the doubt — this is a real, wide band, not a rounding footnote. None of these three candidates should be read as conclusively positive or negative until this band narrows (more resolved trades, and ideally less M30-granularity ambiguity — see the roadmap).

## Sensitivity to cost and execution delay

| Candidate | Avg R (real bid/ask) | Avg R (zero-cost, mid) | Cost impact | Avg R (+30min extra delay) | Delay impact |
|---|---:|---:|---:|---:|---:|
| Baseline | +0.094 | +0.135 | **−0.041 R/trade** | +0.095 | +0.001 |
| Genuine ORB | +0.046 | +0.075 | **−0.029 R/trade** | +0.030 | −0.016 |
| Trend pullback | −0.044 | −0.005 | **−0.039 R/trade** | −0.028 | +0.016 |

Real spread costs every candidate roughly 0.03–0.04R per trade relative to an idealised zero-cost fill — a real, material, correctly-signed effect (never positive, as it shouldn't be). An extra 30 minutes of execution delay moves results by an order of magnitude less (±0.02R) and inconsistently in direction across candidates — on this dataset, cost matters far more than the specific delay assumption chosen.

## By pair and by month

`output/by_pair_*.csv` and `output/by_month_*.csv` (all months in the window included even where a candidate had zero opportunities that month — none occurred in this window, but the harness does not omit a month silently if one ever does). Notable: baseline's by-pair result ranges from USDJPY at +10.2R to USDCAD at −6.0R over the same window — pair selection alone moves the outcome by more than the average-R differences between candidates above, a real dispersion worth keeping in view rather than treating "the baseline" as one number.

## Recommendation (plain English)

- **Baseline:** insufficient evidence either way. Positive in both dev and holdout, but on a small sample (160 entries total) with a wide unknown-outcome band (+0.29 to −0.11). Worth continuing to observe, not worth advancing on this evidence alone.
- **Genuine ORB:** insufficient evidence, leaning toward reject as tested. Strong-looking dev result did not survive into holdout (+0.002 avg R/trade, effectively flat). The larger sample (837 entries) narrows the unknown-outcome band somewhat but doesn't change the holdout picture. Repairing ORB clearly changes signal *volume* a great deal (244 → 1,160 issued) — it does not, on this evidence, change *quality* for the better.
- **Trend pullback:** reject as tested. Negative average R on both the full window and the dev period; the positive holdout period is a small sample (109 resolved) inside a fixed rule set that was net negative overall, not a sign of a real edge.

**None of these is a "ship it" result, and none of the negative results should be read as ruling a mechanism out forever** — this is one fixed rule set per candidate, one dataset, one cost model, tested once, exactly as scoped for this milestone (no parameter sweep, as instructed).

## Roadmap

| Stage | Status | Evidence | Next action |
|---|---|---|---|
| 1. Offline comparison | **Done, this milestone** | This document, `output/ledger_full.csv` (8,191 rows), `output/summary_by_candidate_period.csv`, 3 equity/drawdown plots, 6 harness-correctness checks (`test_replay_correctness.py`, all passing), 5 pre-existing lifecycle test files still passing unmodified | None required to close this stage; see "Issues" below for what a next offline iteration should fix first |
| 2. Prospective observation with timestamped bid/ask | Not started | — | Point a shadow process (reusing `shadow_scanner.py`'s existing precedent — see `RESEARCH_SPEC_ALERT_OUTCOME_AND_ORB.md` §5) at whichever candidate(s) warrant continued observation, scoring with the corrected `src/alert_lifecycle.py`/`src/alert_scorer.py` machinery, collecting real bid/ask at alert time (not just candle-derived) |
| 3. Bounded OANDA practice execution | Not started | — | Only after stage 2 has run long enough to say something about live-feed behaviour, not backtested behaviour |
| 4. Product readiness | Not started | — | A decision point, not an engineering task — depends entirely on what stages 2–3 show |

Engineering progress on this milestone is complete (harness built, run, correctness-checked, reproducible). Strategy performance is separately and explicitly: **insufficient evidence to advance any of the three candidates to stage 2 as-is.**
