# Prospective observation v2 — measurement contract

Written before any v2 code, per the 7-day diagnosis of the v1 run
(`885c5e3ef4b24446a1be3fc05b8f8577`, pinned at `29c286d`): all 8 eligible
trades resolved `incomplete_coverage` with zero completed outcomes. Root
cause — v1 conflated two different things under one 15-second threshold:
*did we keep watching* (collection health) and *has OANDA's own quote
moved* (market tick frequency). This document fixes that conflation. It
does not touch signal generation, confidence, SL/TP, the spread floor, or
position-suppression — only how discretely sampled quotes are measured
into outcomes.

## 1. What each signal actually means (OANDA v20 REST, confirmed against
   developer.oanda.com/rest-live-v20/pricing-df/ and pricing-ep/)

| Signal | Source | What it tells you | What it does NOT tell you |
|---|---|---|---|
| **Receipt time** (`received_at_utc`) | Our own clock, stamped after the HTTP response returns (quote_client.py) | We successfully completed a request at this instant — evidence the collector was alive and the connection was healthy | Nothing about the price itself |
| **Connection health** | Whether the HTTP request succeeded at all, and how | Whether a *request* happened | Whether the *price* changed |
| **Price-creation time** (`time` field, OANDA's own docs: *"The date/time when the Price was created"*) | OANDA, embedded in the response body | When OANDA itself last generated a new price for that instrument | NOT when we received it, and NOT evidence of a problem if it's old — FX price formation is bursty; a quiet instrument can legitimately go tens of seconds between new ticks even with a perfectly healthy connection |
| **`tradeable`** | OANDA, embedded in the response body | Whether OANDA will currently deal that instrument | Distinct from price-creation time — a stale-but-tradeable quote is a different situation from a closed, non-tradeable market |
| **Genuine absence** | No successful response at all for a stretch of receipt-time | We do not know what happened to price during that stretch — collection, not market, evidence | — |

**v1's defect, precisely:** it used price-creation age (OANDA's clock) as
a proxy for collection health (our clock), rejecting a quote outright —
for *every* purpose, including coverage — whenever `time` was more than
15s old relative to receipt. A successful request returning an unchanged,
still-tradeable price is not missing data. It is the correct report of
"nothing new happened." Discarding it manufactured artificial gaps out of
real, healthy, successful observations.

## 2. v2's rule

**Coverage** (did we keep watching) is measured by **receipt-time**
continuity alone: a stretch with no successful response longer than
`COVERAGE_GAP_SECONDS` is a genuine gap — unknown, never bridged, never
guessed at. A successful response, however old its embedded price,
counts as coverage at its receipt time.

**Price-creation age** is retained as **reported metadata on every
sample**, not a validity gate. It is never used to accept or reject a
quote. It is never silently widened to manufacture completed outcomes —
widening it would only have hidden the same conflation one layer deeper
("increase thresholds to obtain completed outcomes," which this contract
exists to *not* do).

A quote is usable to establish entry, exit, or coverage when: it has a
real, finite, positive, non-crossed bid/ask; `tradeable = true`; and its
receipt time is not in the future relative to the scoring clock. That is
all. No price-age gate.

**What this does and does not prove.** A successful, unchanged quote
proves OANDA was reachable and still quoting that instrument as tradeable
at that price, at our receipt time. It does **not** prove the price was
uninterrupted between our polls (a discrete 5s sample stream never could),
and it does **not** prove a broker would have filled an order at that
exact price — both limitations already apply to v1 and are unchanged
here; v2 fixes a false-negative (real coverage misclassified as a gap),
not these pre-existing, honestly-stated limits.

## 3. Outcomes from the sample stream — unchanged framework, corrected gate

Preserved exactly from v1, not reopened here: ask-in/bid-out for longs
(mirrored for shorts), `actual_recording_time_utc` as the entry-window
start, `entry_expiry_utc` as the entry deadline, the original decision's
stop/target/entry-condition levels, chronological one-position-per-pair
suppression, and the 30-hour holding rule as a real elapsed-time bound
from actual entry.

**When a crossing can be reported:** the first sample (by receipt time)
whose bid/ask has crossed stop or target, found by scanning samples in
receipt-time order with no disqualifying receipt-time gap since the
previous checked sample or since entry.

**Observed crossing vs. first crossing — the honesty boundary.** A report
states *"price was observed past this level at this receipt time."* It
never claims *"this was the first instant the level was crossed."*
Between any two 5-second polls the true path is unknown; v1 implicitly
overclaimed "first crossing" by construction (same as every discrete
sampler) without saying so. v2 states the distinction explicitly in the
result: `crossing_type: "observed"` on every resolved sample-based exit,
and the caveat already present in v1 ("price movement between quote
samples is unobserved") is kept and sharpened to name this specifically.

**When the result remains unknown:** a receipt-time gap longer than
`COVERAGE_GAP_SECONDS` anywhere between entry and the last checked sample
→ `incomplete_coverage`, exactly as the concept existed in v1, just
measured on the correct clock. Never bridged. Never guessed at.

## 4. Deadline execution during a closed market — the one new policy

v1's deadline rule (first sample at/after deadline, within
`QUOTE_STALENESS_SECONDS`) cannot be satisfied across a weekend: OANDA
quotes close Friday ~21:00 UTC and reopen Sunday ~21:00–22:00 UTC (varies
with US/UK daylight saving; confirmed empirically from `tradeable`
transitions in the recorded run, not hardcoded as the sole source of
truth). Two of the diagnosed trades' nominal deadlines fell inside that
window and could never have resolved under v1's rule, closure or not.

**Options considered:**
1. *Silently redefine 30 hours as "30 trading hours"* — rejected: changes
   what the contract measures without saying so, and the rejected
   strategies already compared against a *wall-clock* 30h rule.
2. *Fill at the last pre-close quote* — rejected: that price was never
   re-confirmed tradeable at the deadline; it is a historical price being
   used as if it were an execution, which it explicitly could not have
   been (no fill is possible against a closed market).
3. *Carry the position through reopening unlabeled* — rejected: silently
   changes exposure duration without disclosure.
4. **Execute at the first tradeable sample after the market reopens**
   (chosen) — this is the literal executable reality: nothing could have
   filled during closure in a real account either, so the first real,
   tradeable, post-reopen price *is* the honest execution point. The
   nominal nominal 30h deadline is preserved as the trigger; only the
   *execution* moves, and only when market closure (not an unexplained
   gap) is the demonstrated reason.

**This is a new exit policy, not a bug fix**, and is labeled as such in
every affected result (`deadline_delay_reason: "market_closure"`,
`scheduled_exit_time_utc` unchanged, `execution_delay_seconds` reflecting
the real wait). It must be compared separately from v1's wall-clock rule,
never silently substituted into a v1-contract figure. **Closure must be
demonstrated, not assumed**: v2 only applies this rule inside the weekly
closure reference window, and only when demonstrated by affirmative
evidence meeting ALL of the following — mere total absence never
qualifies, by design ("weekend absence alone must remain unexplained"):

- the `tradeable` field is **explicitly `False`** on the evidence quote —
  a key that is simply *absent* (as opposed to present and `False`) does
  not count; a missing flag is a malformed/incomplete observation, not
  OANDA affirmatively reporting closure;
- that quote's `bid`/`ask` are both finite, positive, and non-crossed
  (`bid <= ask`), and its own provider timestamp (`oanda_time_utc`)
  parses — a malformed or crossed quote proves nothing about the market,
  closed or open;
- the same validity bar applies to the reopen sample itself
  (`tradeable` explicitly `True`, well-formed, valid provider timestamp)
  before it is accepted as ending the closure.

**The interval closure actually explains is bounded, not open-ended.**
One genuine piece of evidence near the start of a long silence does not
excuse silence of unlimited length: the candidate reopen sample must
arrive within `MAX_REOPEN_DELAY_FROM_EXPECTED_HOURS` of the *expected*
reopen point (the next weekly reopen on/after the gap's start), not
merely somewhere within the outer `MAX_CLOSURE_DEADLINE_DELAY_HOURS`
sanity ceiling. A real Friday `tradeable: false` observation followed by
silence that continues well past the expected Sunday reopen (e.g. into
Monday) stays `incomplete_coverage` for the unexplained remainder — the
one observation explains the closure, not an arbitrarily longer absence
that happens to follow it. An unexplained gap of similar length on a
weekday, with no such evidence at all, is likewise still
`incomplete_coverage`, never silently treated as closure.

The publisher's bounded per-pair quote window (`research_snapshot_
publisher.py`) is widened by `MAX_CLOSURE_DEADLINE_DELAY_HOURS` for any
contract version that defines it (version-tolerant; v1 has no closure
concept and gets no widening) specifically so this evidence — the
closure quotes and the reopen sample, however late within the sanity
ceiling — is never silently dropped from the bounded-memory path while
still resolving identically to direct scoring of the complete data.

## 5. What v2 explicitly does not change

Baseline signal generation (`technical_score`/`combine_signal`), position
suppression, entry validity window, original stop/target/entry-condition
levels, the spread floor, and the definition of a completed trade's R.
Only the measurement layer between a recorded decision and its sampled
outcome.
