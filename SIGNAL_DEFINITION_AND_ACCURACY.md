# What a "signal" is, and how we prove it's worth trusting

Decision doc from the 2026-08-24 discussion on what a subscriber should
actually receive, PESTLE's role, and building a real accuracy record. Ties
directly into [SIGNAL_IQ_GAP_ANALYSIS.md](SIGNAL_IQ_GAP_ANALYSIS.md) item
1c (performance/track-record aggregation) — this doc is what sharpens that
item into concrete requirements.

## The product framing this settles

A subscriber shouldn't need to understand TA or PESTLE to get value: they
subscribe, get notified, look at one clear read on the pair, decide whether
to trade. That's the bar everything below is designed to meet — without
losing the two things that make the read trustworthy: the ability to
explain *why* it fired, and a real, visible record of whether it's usually
right.

## Decision 1 — keep the tech/PESTLE split internally; present one number

**Computation stays as it is.** `combiner.py`'s
`combined_score = 0.6·tech + 0.4·pestle` and, importantly, its
**disagreement veto** (strong technical + strong PESTLE pointing opposite
ways forces `no_trade` rather than averaging into a diluted call) both
stay. No code change here.

Why the veto matters enough to keep: if ORB + trend + pattern all say "buy"
but PESTLE says "strong negative" (a surprise rate move, sanctions,
whatever), folding PESTLE into one flat weighted average with the three
technical components means three votes outweigh one and the system still
fires a moderate buy — straight into the one scenario where a subscriber
most needs to be told "sit this out," not handed a diluted number. Losing
that protection to simplify the math isn't worth it.

**Presentation changes.** What a subscriber sees is one headline: direction
+ confidence (and later, a calibrated probability — Decision 3), not "tech
score" and "pestle score" as two things to reconcile themselves. This
doesn't undo the Live-feed card design we already agreed on (full technical
breakdown + PESTLE evidence headlines, always visible) — that content stays,
it's just the *explanation* underneath the headline, not the headline
itself. Headline answers "what should I know"; the breakdown answers "why."

## Decision 2 — two accuracy metrics, not one, both from the same data

**2a. Trade-outcome accuracy (R-multiple / win-rate)** — already mostly
built. `eod_brief.py`'s `score_signal()` checks a fired signal's actual
SL/TP against real subsequent OANDA price action and tags it
target/stop/unresolved with an R-multiple. Gap analysis 1c already covers
porting this off Dukascopy onto `fetch_oanda_candles()`, scheduling it on
the droplet, and aggregating across days instead of one Mac-only HTML file
per day. No new design needed here, just deployment.

**2b. Simple directional accuracy** — new, smaller, and this is the metric
that maps directly onto what was described in chat: did price move the
*called direction* by a fixed horizon (e.g. end of trading day, or N hours
post-fire), independent of whether the specific SL/TP got hit. This is:
- Easier to market truthfully ("right 71% of the time") than an R-multiple.
- Easier for a non-trader subscriber to understand.
- The dataset a real probability calibration (Decision 3) gets checked
  against — R-multiple outcomes depend on the specific SL/TP levels chosen;
  directional accuracy doesn't, so it's the cleaner ground truth for "was
  the call right."

Both metrics read from the same `signals_log/` entries + real OANDA
candles — one aggregation job produces both, not two separate builds.

## Decision 3 — "probability" requires calibration, not relabeling

`combined_score` today is an **uncalibrated heuristic** — a weighted sum of
mostly-binary rule votes, bounded to `[-1,1]`. Nothing about it has been
checked against what price actually did afterward. Calling a `+0.6` score
"73% probability of rising" without that check would be a made-up number
wearing a real one's clothes.

The fix, once 2b has produced enough resolved signals: bucket historical
signals by `combined_score` range (deciles, or fixed bins) and compute the
actual historical up-move rate per bucket. *That's* a real calibration
curve — "signals scoring +0.5 to +0.7 have historically been followed by a
same-day up-move 68% of the time" is a claim backed by data, not a
relabeled formula.

**Until that dataset exists**, keep showing direction + confidence tier
(high/medium/low) as today — not a fabricated percentage. How large a
sample is "enough" to trust a calibration bucket isn't decided here; it's a
future checkpoint once 2b is running and we can see how the data actually
distributes (a few hundred resolved signals per pair is a reasonable rough
floor to start sanity-checking against, not a hard requirement).

## A useful side effect worth noting

Once 2b (directional accuracy) is running, it can also **validate Decision
1's veto rule** with real data later: were the technical-only setups that
got vetoed by conflicting PESTLE evidence more likely to have failed than
the ones that weren't vetoed? That's answerable from the same audit trail,
and it's the right way to eventually revisit the veto — with evidence,
not intuition. Not needed now; just worth knowing the infrastructure being
built here sets that up for free.

## What actually changes near-term

- **No changes to `combiner.py`** — today's decision is about presentation
  and audit infrastructure, not the scoring math.
- **Sharpens gap analysis item 1c**: port + schedule `score_signal()`
  *and* add the directional-accuracy computation (2b) alongside it, both
  feeding the same aggregation.
- **Feeds the Performance tab**: both the trade-outcome stats (win
  rate/avg R, already in the prototype) and a directional-accuracy stat
  belong there.
- **Feeds the dashboard headline treatment**: Live feed cards keep their
  full breakdown; the badge/headline simplifies toward "direction +
  confidence" now, "direction + calibrated probability" once Decision 3's
  dataset exists.
