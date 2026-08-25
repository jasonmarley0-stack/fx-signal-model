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

- [x] **Daily performance review cadence** — done. A scheduled cloud
      routine ("Signal IQ daily performance review") runs at 06:00 UTC
      daily and pushes a notification with health + performance numbers.
      Getting there needed more than the API call originally planned — see
      "How the daily review actually works" below for what changed and
      why, since it's worth understanding if this ever needs debugging.
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
- [x] TLS on the dashboard — done as a side effect of building the daily
      review (see below): Caddy fronts the dashboard with automatic HTTPS
      at `https://159-65-19-136.sslip.io`, port 8080 closed externally.
      Swap the Caddyfile's hostname for a real domain later if one gets
      registered; everything else stays the same.
- [ ] Git identity on the Mac — fixed on the droplet this session; the Mac
      side still auto-detects from username/hostname. Cosmetic.

## How the daily review actually works

You picked full automation ("I run a scheduled daily check"). The first
attempt — a scheduled cloud routine calling the dashboard's API directly —
didn't work, and it's worth recording why in case this ever needs
debugging: **cloud routine sandboxes sit behind a strict outbound
allowlist that doesn't include arbitrary third-party hosts.** Confirmed by
testing directly, twice: a plain-HTTP call and then an HTTPS call (behind
Caddy, with a real Let's Encrypt cert) to the droplet both got a flat 403
from the sandbox's own outbound proxy gateway, before the request ever
left the sandbox. TLS and a real hostname don't fix this — it's a platform
policy, not a protocol problem.

The actual fix: route through GitHub instead, since routines already have
sanctioned repo access.

1. `setup/push_status_snapshot.py` runs daily at **05:45 UTC** on the
   droplet (systemd timer) — reads the same local files
   `dashboard_server.py` reads, plus systemd service/timer status, and
   commits+pushes `status/daily_status.json` via a dedicated write-scoped
   deploy key (`fx_signal_model_status_relay`, added to the repo with
   write access — separate from the Mac's own push access and from the
   read-only key used for the private `signal-engine` repo).
2. The "Signal IQ daily performance review" routine fires at **06:00 UTC**
   (7am London, currently BST — will drift to 6am local once clocks go
   back in late October since cron is fixed UTC; revisit then if it
   matters), reads that file from its own repo checkout — no network call
   to your infrastructure at all — and pushes you a notification every
   time, healthy or not.
3. Manage the routine at
   [claude.ai/code/routines](https://claude.ai/code/routines), or ask me
   to check/adjust it in a session like this one (`trig_01UHKBcHN3w2UgDXPqqqc4fH`).

Side effects worth knowing about, both net positives: the dashboard is now
on real HTTPS (`https://159-65-19-136.sslip.io`, port 8080 closed
externally) instead of plain HTTP, and `/api/health` + `/api/performance`
accept a separate `MONITOR_API_KEY` header so automation never needs your
own dashboard login.
