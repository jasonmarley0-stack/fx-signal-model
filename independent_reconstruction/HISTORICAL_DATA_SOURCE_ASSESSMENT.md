# Historical Data Source Assessment — v2 Independent Trade Reconstruction

**Scope:** EUR/USD, GBP/USD, USD/JPY, USD/CAD, USD/CHF, NZD/USD, AUD/USD; 2026-10-26 through 2026-11-21.
**Date of assessment:** 2026-11-24. All findings below are from direct, read-only tests run today
(GET requests only; no orders, no accounts created, no purchases made), not from documentation alone.

## Summary

| Source | Verdict | Why |
|---|---|---|
| **OANDA practice v20 API** | **Selected — primary** | Already integrated, already credentialed, free, GET-only, confirmed complete for the exact required pairs/dates/granularity, native separate bid/ask, same provider the production observer and the prior gap-evidence audit already use. |
| **Existing Signal IQ raw observations (v1 observer's `quotes_log.jsonl`)** | **Selected — secondary/cross-validation** | Free, already collected, independently polled (separate process from v2's own collector), covers the entire required window right now. Cannot fill a gap OANDA's own historical candles can't already fill, but is a genuine independent corroboration source at zero additional cost or risk. |
| Dukascopy historical tick data | **Assessed, not usable from here** | Already has a built-in downloader in this repo (`src/data/dukascopy.py`); confirmed today that its CDN is unreliable from both this sandbox and the droplet (rate-limited, then connection timeout, on two different networks). The existing code's own docstring already flags the same restriction. Would need testing from an unrestricted network before it could be used at all. |
| HistData.com | **Assessed, not recommended** | No official API — manual, form-driven ZIP downloads only, impractical to automate across 7 pairs × 4 weeks. Documented data-quality caveats: timestamps are New York local time with DST (not fixed UTC offset, and not fixed-fractional precision over time), no volume field, no warranty. |
| Twelve Data | **Assessed, not recommended, not signed up for** | The public demo key does not serve the requested historical window at all — it returns the *current* live bars regardless of the date requested (confirmed empirically today), so a genuine historical query needs a registered free account. Free tier is explicitly licensed for evaluation/development only, not for backing this kind of analysis-and-decision work. Forex data on the free tier is a single OHLC series (midpoint-style), not native separate bid/ask — a worse fit for the V2 contract's bid/ask side convention than OANDA. **No account was created** — that would require your explicit approval first. |
| Other sources (Alpha Vantage, Polygon, etc.) | **Not investigated further** | No existing integration or credentials in this codebase; OANDA already fully satisfies the requirement, so there was no reason to create new accounts for this task. |

## 1. OANDA v20 API (practice environment)

- **Availability for required pairs/dates:** confirmed directly. Probed `EUR_USD, GBP_USD, USD_JPY, USD_CAD, USD_CHF, NZD_USD, AUD_USD` at S5 granularity on 2026-10-26 (start of window) and 2026-11-20 (end of window, Friday midday) — every pair returned full, complete 5-second bars (44–60 per 5-minute probe, matching expected density). Probes during the weekly closure (2026-11-01 00:00 UTC, a Sunday; 2026-11-20 23:55→2026-11-21 00:00, Friday evening into Saturday) correctly returned **zero** candles — consistent with genuine market closure, not a data gap.
- **Granularity:** S5 (5-second) OHLC, confirmed working; the API supports down to S5 and up to monthly.
- **Bid/ask:** native, requested via `price=BA` — both sides returned per candle, exactly matching what the V2 contract needs (bid for long exits, ask for short exits).
- **Timestamp precision/timezone:** candle `time` is UTC, nanosecond-precision ISO 8601 with a `Z` suffix (confirmed parseable by Python 3.11+ `datetime.fromisoformat`, which this project's audit tooling already relies on).
- **Completeness:** every candle carries its own `complete` flag; incomplete (still-forming) candles are already filtered out by the existing `fetch_gap_candles()` helper.
- **API limits:** 5,000 records per request (confirmed via public docs; the existing `research_evidence_audit.py` already bounds requests to 4,990 S5 bars = 24,950s per call, under this limit); 120 req/s rate limit — far above anything this exercise needs.
- **Cost:** free on a practice account; this project is already fully credentialed for it.
- **Licensing:** no redistribution restriction relevant to internal research use; this is the same account and environment the production prospective observer and the prior supplementary-evidence audit already use.
- **Compatibility with the V2 measurement contract:** exact — same provider, same side convention, same candle-envelope-proof technique already validated and shipped in `research_gap_evidence.py`/`research_evidence_audit.py` for the three originally-unknown v2 trades (see `BOUNDED_RESEARCH_REPAIR.md`).

**Conclusion:** this is not just the best option — for the specific dates in question, empirical testing today found it is the *only* source that reliably returns usable data at all (see below). It will be the sole historical-price source for this reconstruction.

## 2. Existing Signal IQ raw market observations

The v1 prospective observer (`research-health-publisher`'s own collector, a separate process from v2's) has been running continuously since 2026-09-30 and is still active today, independently polling the same 7 pairs via the same OANDA practice feed. Its `quotes_log.jsonl` (1.47 GB as of today) fully covers the 2026-10-26–2026-11-21 window.

This cannot resolve anything OANDA's own historical candles can't already resolve (same provider, same underlying price stream), but because it was collected by an *independently scheduled* process, a moment where v2's own collector had a gap is not necessarily a gap in v1's log too. It costs nothing extra to consult and never requires a new network request. **Use:** a secondary corroboration signal only — if v1's log happens to show a tradeable sample inside a v2 gap, that is additional, already-existing evidence; it is never a substitute for the OANDA historical-candle proof requirement itself.

## 3. Dukascopy (existing `src/data/dukascopy.py`)

This repo already contains a complete, tested tick-data downloader (LZMA `.bi5` parsing, retry/backoff, OHLC aggregation) with its own synthetic round-trip test (`tests/test_dukascopy.py`). Its own docstring already warns: *"this sandbox's network egress does not have datafeed.dukascopy.com allowlisted (every request returns a 403 at the proxy)."*

Tested today from two separate networks (this local sandbox, and the production droplet):
- First request: HTTP 503, body `{"error": "Too Many Requests..."}`.
- Retry after 15s: HTTP 000 — TCP connection timed out during the TLS handshake (DNS resolved fine; the connection itself was dropped), on **both** real-world-dated (2024) and in-window-dated requests.

This is consistent with the existing code's own caveat (reachability varies by network and is evidently not currently reliable from either environment available here). **Not used for this exercise.** If reconstruction coverage from OANDA proves insufficient later, this should be re-tested from an unrestricted machine — the downloader code is already there and already tested.

## 4. HistData.com

No official API exists; data is distributed as manually-downloaded ZIP archives (one per instrument per month for tick data). This alone makes it impractical to automate across 7 pairs × 4 weeks without a lot of brittle scraping against a site with no programmatic contract. Independently of that, documented data-quality issues make it a worse fit than OANDA even if automated: tick timestamps are New York local time observing DST (not a fixed UTC offset), timestamp precision is not stable over time (bulk duplicate-timestamp rates vary month to month), no volume field, and the provider explicitly disclaims any warranty on the free data. **Not selected.**

## 5. Twelve Data

Reachable (HTTP 200), and the public `apikey=demo` endpoint does return a response — but it ignores the requested date range entirely and returns the *current* live 1-minute bars (observed `datetime` in the response matched today's wall-clock time, not the requested historical window). Getting real historical data for the required dates would need a registered free account. Per the published terms found today, the free tier: allows 800 requests/day and 5,000 points/request (generous enough in principle), but is **explicitly licensed for evaluation and development only** — not appropriate to rely on for a research exercise whose output may inform real decisions — and its forex series is a single OHLC price feed (not separated bid/ask), a structurally worse fit for the V2 contract's bid/ask side convention than OANDA's native BA candles.

No account was created and no API key was requested — that is a new-identity action requiring your explicit approval, and it wasn't necessary given OANDA already satisfies the requirement end to end. **Not selected; flagged here only for completeness per the brief's instruction to assess it.**

## 6. Other providers

No other historical FX data provider has existing credentials or integration in this codebase. Given OANDA practice fully and verifiably satisfies every requirement in section 4 of the brief for the exact pairs and dates needed, no further provider accounts were created or evaluated live. This can be revisited if OANDA's own coverage later proves insufficient for a specific trade.

## Reproduction

The probes above were run via short, disposable, read-only scripts (GET requests only, practice environment enforced, no state written) and have been deleted after use — they are not part of the committed reconstruction engine, which has its own tests. To re-run an equivalent check:

```python
from oandapyV20 import API
from oandapyV20.endpoints.instruments import InstrumentsCandles
api = API(access_token=OANDA_API_KEY, environment="practice", request_params={"timeout": 10})
req = InstrumentsCandles(instrument="EUR_USD", params={"from": "2026-10-26T09:00:00Z", "to": "2026-10-26T09:05:00Z", "price": "BA", "granularity": "S5"})
api.request(req)
```
