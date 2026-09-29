# Research specification: can Signal IQ issue a mechanically-followable alert with positive cost-adjusted performance?

Track B — discussion document only. No code in this file changes anything: no weight, threshold, pair, stop, target, live scanner, or public performance display is touched by writing this. Nothing here places an order or activates automation. Kept as a separate commit from the Track A display fix.

## 1. Exact baseline

The baseline is the live H4 scanner exactly as currently configured and deployed, tagged `scanner_version = "H4_majors_baseline_v1"` (see `live_scanner.py`):

| Parameter | Value | Source |
|---|---|---|
| Pairs | EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD | `live_scanner.py:PAIRS` |
| Granularity | H4 (OANDA boundaries 01/05/09/13/17/21:00 UTC) | `live_scanner.py:GRANULARITY` |
| Technical weights | `{orb: 0.5, trend: 0.3, pattern: 0.2}` (composite default) | `strategies/composite.py:DEFAULT_WEIGHTS` |
| PESTLE | Unused — `alpha=1.0`, `pestle_score=0.0` always | `live_scanner.py:ALPHA` |
| Confidence thresholds | `CONFIDENCE_HIGH=0.6`, `CONFIDENCE_MEDIUM=0.35`, `STRONG_AGREEMENT=0.75` | `src/combiner.py` |
| Reward:risk | `rr=2.0` if (high confidence AND magnitude ≥ 0.75) else `1.5`; `sl_near=1.5×ATR`, `sl_far=2.25×ATR`, `tp_near=1.3×ATR`, `tp_far=rr×ATR` | `src/combiner.py` |
| Spread floor | `MIN_RISK_SPREAD_MULTIPLE=10` — widens `sl_near` to at least 10× the static spread snapshot in `src/spreads.py` if ATR alone would be tighter | `src/combiner.py` |
| Deployed since | 2026-09-23T20:24:29 UTC | confirmed via `journalctl`/`systemctl` in the read-only audit |

**Known, already-confirmed structural fact about this exact baseline** (read-only audit, `CODEX_FOLLOWUP_FINDINGS.md` §6): `orb_signal()` is **permanently 0** on H4 under the default London session open, because `compute_opening_range()`'s 30-minute window never aligns with any H4 candle boundary. Confirmed on 6/6 real H4-era alerts and traced end-to-end on one real signal. Direct consequence: with ORB fixed at 0, the maximum achievable `tech_score` is `0.3+0.2=0.5`, which is **below** `CONFIDENCE_HIGH` (0.6) — so "high" confidence and the 2.0 R:R tier are mathematically unreachable on this exact baseline, not just rare. This is the starting fact the three hypotheses below are built around.

**Alert-outcome scoring baseline:** the lifecycle system built on this branch — `src/alert_lifecycle.py` (immutable issue/revise/cancel records), `src/alert_scorer.py` (version-scoped entry/exit scoring), `src/alert_feed_publisher.py` (unified feed publication) — is the scoring mechanism this whole specification assumes, not the older `performance_scorer.py`. See §4 for why.

## 2. Candidate hypotheses (3, on the ORB question specifically, as asked)

**H-A — Repair.** Anchor the opening-range window to an H4-aligned reference (e.g. the H4 bar's own daily first-of-session boundary, or a fixed UTC anchor that coincides with an actual H4 open) instead of `sessions.py`'s London-local 07:00/08:00 UTC default, so `compute_opening_range()` actually captures real bars and ORB becomes a live, non-zero input again.
*Falsifying test:* after the fix, sample `orb` values across a held-out replay of H4 history (§5, Stage 1). H-A is falsified if ORB remains ~0 on the large majority of bars even after the fix (the anchor choice didn't actually solve the alignment problem), **or** if cost-adjusted `avg_r` (§4 metrics) on held-out data with ORB repaired is not better than the unmodified baseline's.

**H-B — Remove, redistribute weight.** Drop ORB from the composite entirely; redistribute its 0.5 weight to trend/pattern (e.g. 0.6/0.4, exact split TBD — see open decisions). This treats the current "ORB always contributes 0" state as already-established fact and asks whether formalizing it (so the confidence thresholds become reachable again) helps.
*Falsifying test:* a concrete, checkable structural prediction — with ORB removed, real "high"-confidence and strong-agreement signals should start appearing in a held-out replay (currently impossible by construction). H-B is falsified if that doesn't happen (the redistribution still doesn't clear 0.6), **or** if cost-adjusted `avg_r` with the redistributed weights is not at least equal to baseline.

**H-C — Replace with an H4-native component.** Swap opening-range-breakout logic (built for a 30-minute intraday session concept) for something actually designed at H4 granularity — e.g. a prior-H4-bar breakout or a volatility-adjusted momentum measure computed directly on H4 bars.
*Falsifying test:* the new component's own standalone predictive value (correlation with subsequent directional movement, measured independently of the composite) must exceed a null/random-direction baseline on held-out data. H-C is falsified if the new component alone shows no better-than-chance relationship to what happens next — no amount of composite reweighting would fix that.

All three are evaluated **only** via the staged path in §5, starting with offline replay — none require a code or config change to production before Stage 1 produces a result.

## 3. Comparison metrics and denominators

Every metric below is computed **per candidate (baseline, H-A, H-B, H-C) separately, never blended**, matching the discipline established in the read-only audits (`FINDINGS_NOTE.md` §4's central finding was exactly that blending scanner versions produces meaningless aggregates).

| Metric | Definition | Denominator |
|---|---|---|
| `issued` | count of `issued`+`revised` lifecycle events | — |
| `actionable` | `issued` versions with a non-zero effective entry window | `issued` |
| `entered` | versions where an entry (assumed or observed — §4) was confirmed | `actionable` |
| `missed_entry_rate` | `expired_no_entry` count | `actionable` |
| `ambiguous_rate` | `ambiguous_intrabar_exit` + `insufficient_data_entry` + `incomplete_coverage` count — a **data-quality** metric, not a performance one; flags how often M30/midpoint granularity itself is inadequate to answer the question | `actionable` |
| `resolved` | `entered` AND outcome ∈ {stopped, targeted, time_exited} | `entered` |
| `win_rate` | `targeted` count | `resolved` |
| `avg_r` (midpoint) | mean `r_multiple`, entry/exit priced off OANDA midpoint candles (what `alert_scorer.py` already computes) | `resolved` |
| `avg_r` (cost-adjusted) | same, but entry/exit priced off real bid/ask once available (§6/§7) — **not computable from existing data**, requires the prospective trial | `resolved` |
| `revision_rate` | `revised` count | `issued` |
| `cancellation_rate` | `cancelled` count | `issued` |
| `fill_accuracy` | \|actual fill − assumed entry price\|, distribution (mean/median/p90) — **only available from Stage 3** | filled orders |

## 4. Alert outcome, defined in terms of the actual published instruction

This is not a new definition written for this document — it is what `src/alert_lifecycle.py` / `src/alert_scorer.py` already implement and test (28 passing tests as of `857c302`), reused directly rather than redefined:

- **Entry validity** — `entry_condition_lo/hi` + `entry_expiry_utc`, closed early by a later revision or cancellation (`effective_entry_window_end`).
- **Assumed vs. observed fill** — currently **always assumed**: `_find_entry()` confirms entry only when a candle's own open lands in the entry condition range, scored against OANDA midpoint. **Observed** fill (a real broker-confirmed price) does not exist anywhere in this system today — it is exactly what Stage 3 (§5) would add.
- **Original stop and target** — the specific version's own immutable levels (`stop`, `target` on that `issued`/`revised` record), never a later revision's, even if the subscriber is assumed to have entered before that revision published (`IMPLEMENTATION_NOTE_ALERT_LIFECYCLE.md` requirement 4, already implemented and tested).
- **Holding-time exit** — `max_holding_time_hours`, anchored to the assumed/observed entry time, not to publication.
- **Revisions / cancellations** — the `issued → revised* → cancelled?` lineage, each version scored independently against what it actually published.
- **Missed entries** — `expired_no_entry` (price never reached the entry condition before the window closed).
- **Spread** — **not currently an observed field.** `bid_ask_observed` is `null` on every record produced today (OANDA's live-scanner client only ever requests midpoint prices). This is the single largest gap between what this system can currently claim and "realistic execution cost" — see §6/§7.
- **Ambiguous candles** — `ambiguous_intrabar_exit` (a bar crosses both stop and target, order undeterminable from OHLC) and `insufficient_data_entry`/`incomplete_coverage` (touched-but-unconfirmed entry, or missing coverage at a time-exit boundary) — reported as their own explicit states, never silently resolved to a win or a loss.

## 5. Staged path

**Stage 1 — Offline replay (no live/practice account touched).** Run each candidate's technical-scoring variant against already-collected historical H4 candle data (re-fetchable from OANDA read-only if more history is needed). Score entirely with the existing midpoint-based `alert_scorer.py` machinery. Produces the full metrics table in §3 except `fill_accuracy` and cost-adjusted `avg_r`. Fully reversible; no code path touches `live_scanner.py`, `alerts.json`, or `performance.json`.

**Stage 2 — Prospective paper observation (no orders).** Reuses the existing, already-precedented shadow-scanner pattern (`shadow_scanner.py` already does exactly this for a different candidate configuration: runs a variant on real live prices, logs what it *would* signal, and explicitly does not touch `streaming_scanner.py`/`alerts.json`/`performance.json`) — pointed at whichever candidate(s) survived Stage 1, instead of the 2026-09-01 H1-reweight config it currently runs. **Difference from the existing `shadow_performance_scorer.py`:** that script reuses `performance_scorer.py`'s old snapshot-based scoring, which the read-only audits found had real gaps (dedup collapsing distinct instances, no revision/cancellation model). Stage 2 should score shadow output with the new `alert_lifecycle`/`alert_scorer` machinery instead, and this is the stage where real bid/ask collection at alert time begins (§7) — closing the biggest gap identified in §4, still with zero execution risk.

**Stage 3 — OANDA practice-only execution trial.** A small, pre-agreed number of alerts from Stage 2 get actually submitted as orders to an OANDA **practice** (demo) account — never live/real money. This is the only stage that calls OANDA's order-submission API at all, and only ever the practice endpoint. Produces `fill_accuracy` and true cost-adjusted `avg_r`. Nothing here activates automation for real subscribers or changes what the live product does.

No stage in this path changes a weight, threshold, pair, stop, target, the live scanner, or the public performance display — those changes, if any candidate is chosen, are a separate, later decision.

## 6. What existing historical data can establish vs. what needs prospective data

**Can be established now, from data already on hand (Stage 1 only):**
- Whether a given trade definition (entry/stop/target/time-exit, as actually published) would have hit stop, target, or timed out, against OANDA's own historical midpoint candles.
- The structural ORB=0 fact itself, and each hypothesis's effect on which confidence tiers become reachable.
- Revision/cancellation frequency patterns — though the current sample is tiny (3 signals total under the H4 baseline as of the last audit snapshot), so this specific metric needs materially more history before it's meaningful.

**Cannot be established without prospective data (Stage 2/3):**
- Realistic execution cost — only a static, single-day spread snapshot (`src/spreads.py`, dated 2026-09-11) exists; there is no time-varying observed spread anywhere in this system.
- Whether a real fill would occur at the assumed entry price at all, or what slippage/requotes would do to it.
- True notification-to-action delay for a real subscriber (currently a stated 0-second assumption, not a measurement).
- Whether OANDA's execution API would accept an order at the modelled levels at all (minimum-distance rules, margin, broker-side rejection) — unknown until Stage 3.

## 7. Minimum additional data to collect

**At alert time** (extends the existing `alert_lifecycle_log.jsonl` schema — additive, no existing field changes):
- A genuine notification-delivery timestamp, once a real delivery channel exists (replacing the current stated 0-second assumption — not proposing to build that channel here, just naming the gap).
- Real bid **and** ask at `calculated_at_utc`, from OANDA's pricing/streaming endpoint (`PricingInfo` or the streaming pricing API) — distinct from `fetch_oanda_candles()`, which only ever requests midpoint (`price="M"`) and cannot provide this. This is a genuinely new data source, not used anywhere in the live path today.
- Spread at alert time (ask − bid), replacing the static snapshot for any per-alert cost calculation.

**During the Stage 3 practice trial** (all per attempted order):
- Order request: order type, requested price, requested stop/target, submission timestamp.
- Broker response: accepted/rejected, rejection reason if any, broker-assigned order/trade ID, response timestamp.
- Fill: actual fill price, actual fill timestamp, slippage (fill − requested).
- Exit: actual close reason (stop/target/manual/time-based), actual exit price and timestamp, realised P&L in account currency.
- Costs: spread actually paid at entry and exit, any commission/financing/swap the practice account simulates, pip/rounding convention used.

## 8. Decisions for Jase and Codex to discuss before implementation

1. Which of H-A/H-B/H-C to run first in Stage 1 — all three are cheap enough there to run in parallel; is that worth doing immediately, or sequence them?
2. H-B's exact weight redistribution (0.6/0.4 trend/pattern is illustrative, not chosen) — needs a decision before Stage 1 can score it.
3. H-A's exact anchor choice for a repaired opening range — several are defensible (H4 bar's own open, a fixed UTC hour, per-pair primary-session logic); this changes what "repaired" even means and should be pinned down before building it.
4. Whether OANDA's current API subscription/account tier actually supports the pricing/streaming endpoint needed for real bid/ask (§7) — not yet confirmed.
5. Whether Stage 2 truly reuses `shadow_scanner.py`'s existing infrastructure (adding a config variant) or needs its own separate script — `shadow_scanner.py` currently exists for an unrelated H1-reweight experiment and its own `shadow_performance_scorer.py` uses the older scoring approach this spec deliberately avoids reusing.
6. What "a small, pre-agreed number of alerts" means for Stage 3 in practice — a specific count/time window should be set explicitly before that stage starts, not left open-ended.
7. Whether all three hypotheses need to clear Stage 1 before any one proceeds to Stage 2, or whether a clear Stage-1 winner can advance alone while the others are shelved.

No hypothesis here has been run, tuned, or implemented. This document proposes how to find out, not an answer.
