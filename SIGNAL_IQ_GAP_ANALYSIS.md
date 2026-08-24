# Signal IQ — gap analysis: current state → dashboard end state

Written after building the [Signal IQ prototype](https://claude.ai/code/artifact/4b9849ad-4530-4c73-a551-a43bd2d8c5df)
(dark, three-tab: Live / Performance / Settings, alert-feed-first). This
document covers two things: why the live dashboard is showing no signals
right now (a real, diagnosed bug), and everything else standing between
today's `dashboard_server.py` and that end-state design.

## 0. Why the live dashboard isn't showing scores/signals right now

Diagnosed directly against the running droplet (2026-08-24, ~21:00 UTC):

- `streaming_scanner.py` **is** working correctly — `live_scan.json` is
  fresh, technical scores are real and non-zero (e.g. GBPUSD pattern +1.0),
  arrows are updating from live ticks.
- **`pestle_score` is exactly `0.0` for every pair, always.** Checked
  Signal Engine directly: across all 8 currencies there are only **6
  published PESTLE signals total**, and every one of them is now older
  than the 72-hour lookback window `pestle_scorer.py` queries with
  (newest is 2026-08-21T14:04, cutoff is 2026-08-21T21:00 — everything is
  stale by hours to days). Zero signals in-window → zero category scores →
  `pestle_score = 0.0` for every currency, every pair.
- With PESTLE contributing nothing, `combined_score = 0.6 × tech_score`
  only, and technical-alone rarely clears the `±0.35` threshold to leave
  `no_trade` — which is what you're seeing: rows full of zeros and
  `NO TRADE` badges.
- Root cause upstream of that: **no recurring RSS ingestion and no
  recurring evidence review cadence.** `git log` shows exactly one batch
  (6 items, manually reviewed) was ever published; nothing has refilled or
  re-reviewed the queue since. Confirmed no cron job and no systemd timer
  exists on the droplet for either RSS pulls or evidence review. This is
  backlog item #1 from the original handoff — it was already known, it's
  just now the thing actively strangling the dashboard, not a
  someday-nice-to-have.

**Status: fixed and verified live**, 2026-08-24:
1. Reviewed all 135 backlogged items and published 6 more genuine PESTLE
   signals (AUD/JPY/EUR/GBP PMI moves, CAD payments-infrastructure) — see
   `setup/process_evidence_batch_2.py` for the full reasoning per item.
2. That surfaced a **deeper issue than staleness alone**: Signal Engine
   dates each signal by its real-world event date (`source_published_at`),
   not by review/publish date — correct design, but it means review lag
   eats directly into the scoring window. Confirmed directly: freshly
   published signals for events 3–4 days old still scored `0.0` under the
   72h window. Fixed by widening the default lookback to 120h
   (`src/pestle/pestle_scorer.py`, `DEFAULT_LOOKBACK_HOURS`) — enough
   runway for a realistic review cadence, while `RECENCY_HALF_LIFE_HOURS`
   (36h) still decays older evidence sharply within that window.
3. Added `setup/rss-ingestion.service` + `.timer` — re-runs
   `register_rss_sources.py` (already idempotent) every 6h so the queue
   keeps refilling. Confirmed no cron/timer existed before; confirmed
   working after deploy (ran once immediately, next run scheduled, will
   survive reboots).
4. Verified live on the droplet after restart: `pestle_score` is now
   non-zero for GBP/EUR/JPY/AUD/NZD (USD/CHF/CAD stayed at `0.0` — zero
   qualifying published evidence for those three, which is correct, not a
   bug). Still mostly `no_trade` across pairs at the moment, which is
   honest: the fresh evidence is real but modest in magnitude, and
   technicals happen to be quiet right now too — not the same failure mode
   as before.

**Still open**: the recurring RSS timer keeps the queue *filled*; nothing
yet keeps it *reviewed* on a cadence. Review is deliberately manual (no
auto-classification, per Signal Engine's design principle), so this needs
a standing habit — e.g. a periodic prompt to run another
`process_evidence_batch_N.py`-style pass — not another piece of
automation. Worth deciding cadence (daily? every few days?) so evidence
doesn't sit long enough to blow past even the widened window again.

One concrete catalog gap found while reviewing: **UK and Canada retail
sales both had clear beat/miss data with no fitting SignalType** —
`CONSUMER_CONFIDENCE_UP/DOWN` is a sentiment-survey type and would be a
stretch for actual spending data. Same shape of gap as the already-known
missing PMI type (composite PMI *was* mapped to `CONSUMER_CONFIDENCE_*`
here, since PMI is itself a business-sentiment survey — a much closer fit
than retail sales is). Worth adding a `RETAIL_SALES_BEAT/MISS` SignalType
alongside the PMI one if this keeps coming up.

## 1. Backend/data gaps

### 1a. PESTLE evidence flow
Covered above. **Highest priority** — everything downstream (alert cards,
combined scores, the product's actual thesis of technical + PESTLE) is
inert without it.

### 1b. Alert payload doesn't carry the full breakdown the design needs
Today, `streaming_scanner.push_alert()` writes only: pair, direction,
confidence, combined_score, entry, message, reason. The Signal IQ card
design needs, per alert: technical components (orb/trend/pattern), PESTLE
category scores per currency, evidence headlines (title + source + time)
per currency, SL/TP range, session window. All of that is already computed
in the recompute loop — it's just discarded before being persisted.

Separately: `RawPestleSignal` (`src/pestle/signal_engine_client.py`)
currently captures `description` (a long raw-HTML blob, confirmed by
querying Signal Engine directly — bylines, `<p>` tags, etc.) but not
`title` or `source_url`. `title` is the actual clean headline text
("Consumer/business confidence rises") the cards want; `description` isn't
fit for card display as-is.

**Action:** extend `push_alert()`'s payload with the full row (tech +
pestle incl. per-category scores + top evidence items) at fire time; add
`title`/`source_url` to `RawPestleSignal` and `SignalEngineClient._fetch_live`;
have `pestle_scorer.py` surface the top 1–2 evidence items per category
alongside the numeric scores.

### 1c. No performance/track-record aggregation — but the hard part already exists
Good news: **`eod_brief.py` already solves signal-outcome scoring** —
`score_signal()` checks a fired signal's actual SL/TP against real
subsequent price action and returns stop/target/unresolved + R-multiple.
The gaps are only in how it's deployed:
- Depends on `backtest.load_real_ohlc` → Dukascopy, which is Mac-only.
- Runs once/day via `launchd`, never on the droplet.
- Outputs a single day's standalone HTML, no multi-day aggregation, no
  rolling win-rate, no cumulative-R series — the exact shape the
  Performance tab needs.

**Action:** port `score_signal()`'s logic to the droplet, swapping
Dukascopy for the already-live `fetch_oanda_candles()`; run it on a
recurring systemd timer (hourly is plenty); persist results to a small
rolling dataset (e.g. `performance.json`, appended to as signals resolve)
instead of just rendering HTML; aggregate into win rate / avg R /
cumulative-R / per-pair breakdown with 7D/30D/All windows.

### 1d. No settings/preferences persistence
Nothing today stores per-pair mute, sound on/off, "high confidence only",
or notification-channel prefs — not even for a single user.

**Action:** minimal for now — a single `settings.json` on the droplet
(matches the single-user-for-now scope we agreed), with GET/POST
endpoints; `push_alert()` should honor muted pairs and "high confidence
only" if set.

### 1e. Dashboard only exposes one table's worth of data
`dashboard_server.py` today has `/`, `/table`, `/alerts` — all tied to one
`live_scan.json`/`alerts.json` pair. Needs `/api/performance` (serving 1c)
and `/api/settings` (GET/POST for 1d) once those exist.

## 2. Frontend/UI gaps
- **Visual system**: none of the Signal IQ tokens/fonts/branding exist in
  `dashboard_server.py` yet. Mechanical work — the prototype is the source
  of truth to build against.
- **Alert feed cards**: don't exist (table-only today). Needs the enriched
  payload from 1b before it can show more than pair/direction/score.
- **Market table**: exists and is already data-compatible — restyle only.
- **Performance tab**: doesn't exist. Needs both 1c (backend) and the tab
  itself.
- **Settings tab**: doesn't exist. Needs both 1d (backend) and the tab
  itself.
- **Nav shell** (sidebar/bottom-nav, three sections): doesn't exist —
  single page today.

## 3. Product/account gaps — deliberately deferred
Per what we agreed: design for multi-user, build against the current
single shared-password login. Only implementation note: keep the
"Jason / Signal IQ Pro" identity block as one swappable data object rather
than hardcoding it in multiple places, so real accounts can slot in later
without a rewrite.

## Suggested sequencing

1. **Fix the root cause (0)** — recurring RSS ingestion + review cadence.
   Nothing else matters if there's no real evidence to show; this alone
   may get the current dashboard producing real signals again within days.
2. **Enrich the alert payload (1b)** — needed before cards can show
   anything beyond what the table already shows.
3. **Port + schedule the eod_brief scoring to the droplet, aggregate (1c)**
   — needed before Performance can show real numbers instead of an empty
   tab.
4. **Settings persistence (1d)** — small, low-dependency, can happen
   anytime before the Settings tab needs to do something real.
5. **Rebuild `dashboard_server.py`** against the Signal IQ visual system
   and three-tab structure, wired to real data from 1–4 — the actual
   "build the dashboard" step, done last so it's not a polished shell
   around data that isn't there yet.
