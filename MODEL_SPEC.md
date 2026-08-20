# FX Signal Model — Architecture Spec

Version 0.1 — draft for review

## 1. Purpose

Produce a daily/intraday trading signal for major FX pairs by combining:

1. A **technical score** from multiple candlestick/price-action strategies run on OHLC data.
2. A **PESTLE score** from automated news/sentiment analysis across six categories.
3. A **combiner** that cross-references both into a final signal (long / short / no-trade), a confidence level, and a stop-loss / take-profit range.

This is a decision-support model, not an execution system. It does not place trades. Every output should be treated as a hypothesis to validate, not an instruction — currency markets are famously hard to beat and this system has no track record yet. Everything below is designed to be backtested and paper-traded before any of it touches a live account.

## 2. Scope (v1)

- **Instruments:** EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD, USDCAD, NZDUSD (the seven "majors"). Configurable list.
- **Timeframes:** primary signal on the 30-min / 1H chart around the London and New York opens; daily PESTLE score refreshed each morning (07:00 Europe/London, ahead of London open).
- **Outputs:** (a) a Python backtester + signal generator, (b) an HTML "morning dashboard" per pair, (c) a later port of the technical layer to Pine Script for TradingView.

## 3. Technical layer

Three independent strategies, each producing a score in **[-1, +1]** (short ↔ long) and a **triggered/not-triggered** flag. Scores are only meaningful when triggered; otherwise the strategy abstains (0, not counted).

### 3.1 Opening Range Breakout (ORB), 30-minute
- Define the opening range as the high/low of the first 30 minutes after a session open (London 08:00, NY 13:30 UK time).
- Long trigger: close breaks above OR-high with volume/momentum confirmation (candle body > 50% of range, or ATR-normalized breakout size > 0.25×ATR14).
- Short trigger: mirror image at OR-low.
- Score = +1 (long breakout), -1 (short breakout), 0 (inside range / false breakout filtered out).
- False-breakout filter: require the breakout candle to close beyond the range, not just wick through it, and require the next candle to hold beyond the range (2-candle confirmation) to cut whipsaws.

### 3.2 Trend-following (EMA/MA + momentum)
- Fast EMA (20) vs slow EMA (50) on the 1H chart for regime; 30-min MACD (12,26,9) for momentum confirmation.
- Score = +1 when EMA20 > EMA50 and MACD histogram is positive and rising; -1 for the mirror; 0 otherwise.
- This strategy is a *filter/context* strategy as much as a signal — it's mainly used to veto ORB trades against the higher-timeframe trend (see §5).

### 3.3 Candlestick pattern recognition
- Detect classic reversal/continuation patterns at significant levels (prior day high/low, round numbers, session opens): bullish/bearish engulfing, pin bar / hammer / shooting star, morning/evening star.
- Score = +1/-1 weighted by pattern reliability (engulfing and pin bar at a tested level weighted higher than an isolated doji), 0 if no pattern or pattern occurs mid-range with no confluence.

### 3.4 Technical composite
```
tech_score = w1*ORB + w2*Trend + w3*Pattern
```
Default weights: `w1=0.5 (ORB), w2=0.3 (Trend), w3=0.2 (Pattern)` — ORB is the primary entry trigger, trend is a directional filter, patterns add confluence. Weights are configurable and should be tuned in backtest, not assumed correct out of the box.

If Trend strongly disagrees with ORB (opposite sign, |Trend| = 1), tech_score is dampened by 50% rather than allowed to net out — countertrend breakouts are lower quality, not simply "average quality."

## 4. PESTLE layer

Six categories, each scored **[-1, +1]** per currency (not per pair — a pair's PESTLE score is `pestle(base) - pestle(quote)`, symmetric to how a currency pair's economics work).

| Category | Example inputs |
|---|---|
| Political | elections, government stability, geopolitical conflict, sanctions |
| Economic | rate decisions, inflation (CPI), employment (NFP-style releases), GDP, trade balance |
| Social | consumer confidence, labor unrest, demographic shifts reported in press |
| Technological | central bank digital currency news, payment infrastructure shifts affecting FX flow |
| Legal | regulatory changes, court rulings affecting trade/finance |
| Environmental | climate policy with trade/energy impact (relevant mainly for commodity-linked currencies: AUD, CAD, NZD) |

### 4.1 Pipeline
1. **Ingest**: pull headlines/articles from configured news APIs (e.g. NewsAPI, GDELT, a financial newswire feed) filtered by currency-relevant keywords and source allowlist (Reuters, Bloomberg, central bank RSS feeds, ForexFactory calendar for economic releases).
2. **Classify**: tag each article into one or more PESTLE categories (keyword/topic model first pass; can upgrade to a small classifier later).
3. **Score sentiment**: run sentiment scoring (finance-tuned model, e.g. FinBERT, or a general LLM sentiment call) to get a per-article polarity in [-1, +1] relative to the currency's strength (not generic positive/negative — "inflation surprises higher" is economically bullish for the currency via rate-hike expectations, even though "inflation" reads as a negative word generically).
4. **Aggregate**: recency-weighted average per category per currency (half-life ~24-48h so yesterday's news fades but doesn't vanish), with a materiality multiplier for scheduled high-impact releases (rate decisions, NFP, CPI) vs routine commentary.
5. **Currency PESTLE score**: weighted sum of the six categories. Economic gets the largest default weight since it's the dominant FX driver; environmental the smallest except for commodity currencies.

Default category weights: `Economic 0.40, Political 0.20, Social 0.10, Technological 0.05, Legal 0.10, Environmental 0.05` (Environmental raised to 0.15, Economic reduced to 0.30 for AUD/CAD/NZD specifically).

### 4.2 Pair PESTLE score
```
pestle_score(PAIR = BASE/QUOTE) = pestle(BASE) - pestle(QUOTE)
```
clipped to [-1, +1].

## 5. Combiner: from two scores to a trade signal

```
combined_score = α * tech_score + (1-α) * pestle_score      (default α = 0.6)
```

- Technical layer is weighted higher by default because it's the higher-frequency, better-tested signal; PESTLE acts as a confirming/vetoing context layer, consistent with how a lot of discretionary day traders actually use fundamentals — as a filter on technical setups, not a standalone trigger.
- **Agreement bonus**: if tech_score and pestle_score have the same sign and both |score| > 0.3, confidence is boosted (see below) — this is the highest-quality setup: price action and fundamentals pointing the same way.
- **Disagreement veto**: if they have opposite signs and both |score| > 0.4, output **no-trade** rather than a diluted signal — conflicting technical and fundamental pictures are exactly when day-trading setups fail most often.
- Signal direction = sign(combined_score); magnitude maps to a confidence tier:
  - |combined_score| ≥ 0.6 → High
  - 0.35–0.6 → Medium
  - < 0.35 → Low / no-trade

### 5.1 Stop-loss / take-profit
Computed from **ATR(14)** on the relevant timeframe (30-min for the trade, 1H ATR referenced for context), not fixed pips — so it adapts per pair/volatility regime:

```
stop_loss   = entry ∓ 1.0 * ATR14
take_profit = entry ± 1.5 * ATR14   (1.5R baseline)
```

- Baseline reward:risk is 1.5:1; widened to 2:1 when confidence is High and technical + PESTLE agree strongly (|combined_score| ≥ 0.75).
- Output is given as a **range**, not a single number: SL range = entry ∓ [0.8×ATR, 1.2×ATR], TP range = entry ± [1.3×ATR, 2.0×ATR] depending on confidence tier — giving room for discretionary placement around key levels (round numbers, prior highs/lows) rather than a mechanically exact price.

### 5.2 Signal time window (GMT)
Every signal carries an explicit validity window in GMT/UTC (`src/sessions.py`), not just a snapshot score:
- Each pair maps to a **primary trading session** (London for EUR/GBP crosses, Tokyo for JPY, Sydney for AUD/NZD, New York for USDCAD) whose open/close in UTC anchors the window.
- `generated_at` = the bar time the signal was produced; `valid_until` = 90 minutes later, capped at that session's close — a signal never claims to be live past the session it was generated in.
- The dashboard shows the window as `HH:MM GMT, DD Mon – HH:MM GMT, DD Mon (Session)` with a LIVE/EXPIRED pill computed at render/view time, so a stale signal is never mistaken for a current one.

## 6. Deliverables & build order

1. Python backtester (`src/`) — strategies, PESTLE scorer (with a mock/offline mode until API keys are wired in), combiner, backtest engine, metrics (win rate, expectancy, max drawdown, Sharpe).
2. Morning dashboard (`dashboard/`) — static HTML generated from the day's model output: per-pair PESTLE breakdown (radar/bar), technical signal, combined signal, SL/TP range, confidence.
3. TradingView Pine Script port of the technical layer (v2, after backtest validation) — PESTLE can't run natively in Pine (no news API access), so the TradingView version will need the dashboard's PESTLE score fed in manually as an input, or use Pine's webhook/alert system to pull it from the Python model.

## 7. Validation plan
- Backtest each technical strategy standalone first (2-3 years of 30-min data per pair) to sanity-check it actually has edge before combining.
- Backtest the combiner against technical-only to confirm PESTLE is adding value, not just noise (this is the most likely place the model disappoints — sentiment scoring for FX is genuinely hard to get right, so treat early PESTLE weights as a hypothesis to disprove, not a given).
- Paper-trade the full signal for a meaningful sample (at least a few dozen trades) before considering real capital.

## 8. Open questions for Jase
- Which news API(s) do you have or want to use (NewsAPI, GDELT, a paid financial feed, LLM-based scoring of scraped headlines)? This determines what "automated" actually costs and how it's wired in.
- Which OHLC data source for backtesting (broker export, Dukascopy, a paid data vendor)?
- Confirm default weights above are a reasonable starting point — they're informed assumptions, not fitted values, and should shift once backtest results come in.
