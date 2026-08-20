# PESTLE layer: integration with Project Signal ("Signal Engine")

Decision (confirmed with Jase): this repo stays separate from Signal Engine and
talks to it over its existing FastAPI API. Signal Engine's evidence/scoring
machinery is reused as-is; nothing in its codebase needs to change to get a v1
PESTLE feed working.

## 1. Mapping FX concepts onto Signal Engine's schema

| Signal Engine concept | FX/PESTLE use |
|---|---|
| `Company` | A currency: GBP, USD, EUR, JPY, CHF, AUD, CAD, NZD (8 rows, one-off setup) |
| `SignalType` | One PESTLE-relevant event class, e.g. `CENTRAL_BANK_RATE_DECISION`, `CPI_RELEASE`, `ELECTION_RESULT`, `SANCTIONS_IMPOSED`, `GDP_RELEASE`, `TRADE_BALANCE`, `EMPLOYMENT_RELEASE`, `GEOPOLITICAL_CONFLICT`, `CENTRAL_BANK_SPEECH`, `CLIMATE_POLICY_ENERGY` — each tagged with a `category` matching one of the six PESTLE letters (Signal Engine's `category` field is currently constrained to its infrastructure vocabulary — see §3, this needs a small config change, not a code change) |
| `Signal` | One piece of published evidence: "BoE raises rates 25bp", linked to `company_id` = GBP's Company row, `signal_type_code` = `CENTRAL_BANK_RATE_DECISION`, with `confidence` and `source_credibility` set per Signal Engine's existing guidance |
| `Opportunity` (per Company) | Repurposed as the currency's aggregate PESTLE score, 0–100. Signal Engine already computes `base_weight × confidence × credibility × recency` and decays recency — exactly the aggregation the model spec calls for in §4.1 |
| `Opportunity.score` (0-100) | Rescaled to the model's [-1, +1] PESTLE score: `pestle = (score - 50) / 50`. This assumes SignalTypes are entered with a *directional* base_weight sign convention (see §2) — Signal Engine's weights are currently unsigned (1-100, all additive), so this needs the convention below rather than a code change. |

## 2. Handling direction (bullish vs bearish for the currency)

Signal Engine's `Opportunity` score is a **magnitude** (0-100, all contributions
additive) — it was designed for "how much commercial opportunity" not "which
direction". FX needs signed direction. Two ways to get it without touching
Signal Engine's core scoring code:

**Chosen approach — two SignalTypes per event class, opposite polarity:**
e.g. `CPI_ABOVE_FORECAST` (hawkish/bullish for the currency) and
`CPI_BELOW_FORECAST` (dovish/bearish), each posted as a normal `Signal` against
the same Company. Then compute direction on the FX side (not inside Signal
Engine) as:

```
bullish_score = sum of contributions from *_BULLISH-tagged SignalTypes published in the lookback window
bearish_score = sum of contributions from *_BEARISH-tagged SignalTypes published in the lookback window
pestle(currency) = clip((bullish_score - bearish_score) / 50, -1, 1)
```

This keeps Signal Engine completely untouched — the FX client just calls
`GET /opportunities/{id}/evidence` (or `/companies/{id}/timeline`), reads each
Signal's `signal_type_code`, and buckets by a small polarity lookup table
maintained on the FX side (`PESTLE_SIGNAL_POLARITY` in `signal_engine_client.py`).
Category (P/E/S/T/L/E) comes along for free from each SignalType's tag once
that field's controlled vocabulary is extended (§3).

## 3. One-time setup needed on the Signal Engine side

These are additive, backwards-compatible changes made entirely through Signal
Engine's REST API — **Jase asked that Signal Engine's own codebase not be
modified**, so this repo does not touch it. An earlier draft of this doc
proposed adding five new members to Signal Engine's `SignalCategory` Python
enum (`app/models/signal_type.py`); that's been dropped in favour of the
approach below, which needs zero code changes on the Signal Engine side.

**How PESTLE categorisation works without changing Signal Engine's enum:**
each PESTLE SignalType we seed is given the closest-fitting value from Signal
Engine's *existing* category vocabulary (`recruitment, land, planning,
policy, regulation, supply_chain, investment, leadership, construction,
utilities, environmental, commercial`) purely so it satisfies Signal Engine's
own validation — e.g. `RATE_HIKE` is filed under `investment`,
`GOVERNMENT_STABILITY_NEGATIVE` under `policy`. That value is never used for
our actual P/E/S/T/L/E bucketing. The *real* PESTLE category for each
SignalType lives on the FX side, in two places kept in sync: a
`pestle_category` field in `setup/seed_pestle_signal_types.json` (informational,
stripped out before POSTing) and the authoritative
`PESTLE_SIGNAL_POLARITY` lookup table in `src/pestle/signal_engine_client.py`,
which is what `pestle_scorer.py` actually reads. So Signal Engine just stores
and serves evidence as it always has; all PESTLE-specific meaning is applied
in this repo.

1. Create the PESTLE `SignalType` rows via `POST /signal-types` (see
   `setup/seed_pestle_signal_types.json` — `setup/seed_signal_engine.py`
   strips the FX-only `pestle_category` field before sending). Base weights
   are a starting hypothesis, tune after backtesting.
2. Create 8 `Company` rows, one per currency (see `setup/seed_currencies.json`).
3. Register a news connector. Signal Engine already has a working RSS/Atom
   connector — feed it **financial-news-specific sources, not Signal Engine's
   existing industry (UK infrastructure) feeds**, which are the wrong domain
   for FX. Use `setup/rss_feeds_financial.json`: verified central bank press
   feeds (Fed, ECB, BoE, Bank of Canada, SNB, RBA) plus general market
   newswires (InvestingLive/ForexLive). A few entries in that file
   (`verified: false`) — BoJ, RBNZ, FXStreet, Investing.com's forex-specific
   feed, Bloomberg — need a quick manual check before registering, either
   because their RSS URL structure couldn't be confirmed from this session or
   because the fetch was blocked by robots.txt; Reuters no longer offers
   public RSS at all and would need a paid data licence. These can all be
   registered as `EvidenceSource` rows with zero new connector code. A
   dedicated NLP-classified news API connector (NewsAPI/GDELT + sentiment) is
   a genuinely new connector and is the one piece of net new work — everything
   else above is configuration.

## 4. What the FX repo actually does

`src/pestle/signal_engine_client.py` — thin REST client: fetch a currency's
recent published Signals from Signal Engine, bucket by PESTLE category and
polarity, compute the [-1, +1] score per currency, then combine base/quote
into the pair score (`pestle(BASE) - pestle(QUOTE)`, per the model spec).

Runs in **mock mode** by default (bundled sample fixtures) so the backtester
and dashboard work today without Signal Engine running or seeded — swap
`SIGNAL_ENGINE_BASE_URL` in `.env` once the setup in §3 is done.
