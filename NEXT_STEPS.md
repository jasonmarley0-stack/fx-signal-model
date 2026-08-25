# Next steps — checklist

Written 2026-08-25, after the MVP went live: streaming scanner (28 pairs),
PESTLE evidence pipeline, alert feed with full breakdown, performance
scoring (trade-outcome + directional accuracy), and the Signal IQ
dashboard (Live + Performance tabs). This is the live tracker for what's
left — it supersedes the "suggested sequencing" section in
[SIGNAL_IQ_GAP_ANALYSIS.md](SIGNAL_IQ_GAP_ANALYSIS.md), which is now
mostly done and stands as a historical record instead.

## Shipped this session (for context)

- [x] Diagnosed and fixed why PESTLE scores were stuck at 0 (review-lag vs.
      lookback window); widened the window; scheduled recurring RSS
      ingestion.
- [x] Streaming scanner live on the droplet, real PricingStream, verified
      reconnect-on-drop.
- [x] Alert payload enriched with full technical + PESTLE breakdown
      (evidence headlines, not just scores); rendered as real Live Feed
      cards.
- [x] Performance scoring: trade-outcome (R-multiple) + directional
      accuracy, both scheduled hourly, both surfaced on a real Performance
      tab with per-signal detail (fire time, call, outcome).
- [x] Live tab per-pair sparklines.
- [x] Expanded from 7 pairs to all 28 crosses among the 8 tracked
      currencies.
- [x] Dashboard rebuilt onto the Signal IQ visual system throughout.

## Outstanding — operating the MVP over the next 30 days

- [ ] **Daily performance review cadence — needs a decision, see below.**
      This is the one thing worth explicitly agreeing on before calling
      this phase "running," not just building more.
- [ ] **PESTLE evidence review cadence.** RSS ingestion is now automatic
      (every 6h) but review (raw evidence → published signal) is still a
      manual/Claude-assisted pass with no set schedule — deliberately so,
      per Signal Engine's no-auto-classification design. Concrete evidence
      this needs a cadence now, not eventually: the queue grew from 141 to
      175 items since the timer went in, and *unreviewed* items grew from
      135 to 163 despite publishing 6 more in between. Ingestion is now
      outpacing review.
- [ ] **Watch alert volume/noise at 28 pairs.** 7 pairs already produced a
      rapid confidence-flicker episode (NZDUSD, 4 alerts in 2 minutes on
      one wobbly setup). 28 pairs will surface more of this. Worth keeping
      an eye on whether the Live Feed stays useful, or whether Settings'
      mute/high-confidence-only controls (below) become urgent sooner than
      planned.
- [ ] **Directional-accuracy calibration checkpoint** (see
      [SIGNAL_DEFINITION_AND_ACCURACY.md](SIGNAL_DEFINITION_AND_ACCURACY.md)).
      Not actionable yet — revisit once a meaningful sample of resolved
      signals exists (rough floor: a few hundred per pair) to turn
      `combined_score` into an actual calibrated probability instead of an
      unvalidated heuristic.

## Outstanding — features discussed

- [ ] **Settings tab** (gap analysis item 1d): per-pair mute, sound
      toggle, "high confidence only." No persistence layer exists at all
      yet — needed both for its own sake and because it's the direct
      answer to the noise concern above.
- [ ] **Early warning system / economic calendar integration.** Partially
      scaffolded already, currently disconnected from the live product:
  - `src/calendar_events.py` (parses forward-looking events),
    `setup/seed_calendar_events.py` (posts them to Signal Engine as
    `SCHEDULED_HIGH_IMPACT_EVENT`), and `setup/economic_calendar.json`
    (the actual event data) already exist from the original Mac-only
    build.
  - Investing.com has no public API/RSS (confirmed at the time this was
    built) — refreshing the calendar is a manual/Claude-assisted WebFetch
    pass against investing.com's calendar page, the same non-automatable
    shape as PESTLE evidence review, not something a cron job can do.
  - `economic_calendar.json` currently holds exactly 2 entries from the
    original setup (a September FOMC decision, a UK Autumn Budget date) —
    hasn't been refreshed since and both are the only events tracked.
  - None of this is wired into the live droplet dashboard or
    `streaming_scanner.py` today — it was built for the old Mac-only
    dashboard (`dashboard_data.py`'s `upcoming_events_for_pair`), which
    isn't what's running in production.
  - Building this for real needs: (a) a refresh cadence for the calendar
    data, (b) wiring `calendar_events.py` into the live scanner/dashboard,
    (c) a decision on what it actually surfaces — e.g. a banner ahead of
    time ("high-impact USD event in 3h") on affected pairs, distinct from
    PESTLE's after-the-fact evidence scoring.

## Known gaps, lower priority

- [ ] Add `PMI_BEAT`/`PMI_MISS` and `RETAIL_SALES_BEAT`/`RETAIL_SALES_MISS`
      SignalTypes to Signal Engine's catalog — found real evidence for
      both during the evidence review pass that had to be approximated
      into `CONSUMER_CONFIDENCE_*` (PMI, a reasonable fit) or skipped
      entirely (retail sales, not a good fit for a sentiment-survey type).
- [ ] TLS on the dashboard (currently plain HTTP on a public port) — was
      already flagged as "before this becomes anything resembling a real
      product with real users." Worth revisiting now that real performance
      numbers are live, even before other users exist.
- [ ] Git identity on the Mac — fixed on the droplet this session; the Mac
      side still auto-detects from username/hostname. Cosmetic.

## The key decision: daily performance review

Three ways this could actually work, from least to most automated:

1. **You check the Performance/Live tabs yourself**, on whatever cadence
   suits you; I'm available on demand if something looks off or you want
   a deeper read on a specific day.
2. **I run a scheduled daily check** (via a scheduled cloud task) that
   reads `performance.json` + service health each day and sends you a
   short summary — win rate/avg R/directional accuracy movement, any
   service down, any error spike — without you having to go look.
3. **Hybrid**: I run the automated *health* check (services up, signals
   firing, nothing erroring) on a schedule, but the actual performance
   numbers are something you review yourself on your own cadence, since
   30 days of data is too early to react to day-to-day noise anyway.
