# Running the real Dukascopy backtest

These steps run the technical-layer backtest against actual historical FX
prices instead of the synthetic data used in the demo. Do this from your own
machine (or any environment with normal internet access) — the build
sandbox this project came from could not reach Dukascopy's servers, so this
path is unverified end-to-end; the pieces that don't depend on the network
(the tick-format parser) are unit-tested and known to work.

## 1. Unzip and set up the environment

```bash
unzip fx-signal-model.zip
cd fx-signal-model
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install pandas numpy httpx
```

## 2. Confirm you can reach Dukascopy

```bash
curl -sI "https://datafeed.dukascopy.com/datafeed/EURUSD/2026/00/03/00h_ticks.bi5"
```

You want an `HTTP/1.1 200` (or `404` for an hour with no ticks, e.g. a
weekend hour — that's also fine). If you get a `403` or the connection
hangs, something on your network (corporate firewall, VPN, ad-blocker
DNS) is blocking it — try a different network or check your firewall
rules for that domain.

Note the month in the URL is **zero-indexed** (`00` = January) — that's
Dukascopy's format, not a typo, and the code in `src/data/dukascopy.py`
already handles this for you.

## 3. Run the parser unit tests (sanity check before touching real data)

```bash
python3 tests/test_dukascopy.py
```

Should print `All dukascopy parser tests passed.` This confirms the
tick-decompression and OHLC-bucketing logic is correct, independent of
whether the network call works.

## 4. Run a real backtest

```bash
cd src
python3 backtest.py EURUSD --start 2026-01-01 --end 2026-02-01 --freq 30min
```

- `symbol` — a Dukascopy instrument code, no slash: `EURUSD`, `GBPUSD`,
  `USDJPY`, `USDCHF`, `AUDUSD`, `USDCAD`, `NZDUSD` all work.
- `--start` / `--end` — ISO dates (`YYYY-MM-DD`). Start with a short window
  (a few weeks) the first time — see the timing note below.
- `--freq` — pandas resample frequency for the OHLC bars. `30min` matches
  the model's default ORB timeframe; try `1H` or `15min` to compare.

First run for a given symbol/date range downloads and caches every hour's
tick file under `data_cache/<SYMBOL>/<YYYY>/<MM>/<DD>/`. Re-running the same
range afterwards reads from that cache and is much faster — delete the
relevant folder under `data_cache/` if you ever want to force a re-download.

**Timing expectation:** Dukascopy serves one file per hour per symbol.
A month of 24/5 FX trading is roughly 500 hourly files, and each is a small
request — expect low tens of seconds to a couple of minutes for a month,
scaling roughly linearly with the date range. A full year will take a while
the first time; the on-disk cache means you only pay that cost once.

## 5. Read the output

```
EURUSD 2026-01-01 to 2026-02-01 (30min bars)
Trades: 44
Win rate: 52.3%
Expectancy: +0.18R per trade
Max drawdown: 3.40R
```

- **Win rate** — % of trades that closed positive.
- **Expectancy** — average result per trade in R-multiples (R = the ATR-based
  risk unit from `combiner.py`, i.e. how many "stop-losses" you made or lost
  on average per trade). Positive expectancy is the bar for "worth
  investigating further," not "ready to trade" — see the checks below.
- **Max drawdown** — the worst peak-to-trough cumulative loss in R, over the
  whole test period.

## 6. What to actually do with the result

This is one backtest on one pair over one window — treat it as a first
data point, not a verdict. Before trusting it:

- **Run it across all 7 majors** and at least 6–12 months of history each.
  A strategy that only works on EURUSD in a one-month window isn't a
  strategy, it's a coincidence.
- **Compare against a longer max_hold_bars / different entry_threshold** —
  the defaults in `backtest.py` (`entry_threshold=0.35`, `max_hold_bars=12`,
  `rr=1.5`) are the same starting-point assumptions from `MODEL_SPEC.md`,
  not fitted values. Sweep them and watch for a result that's only good at
  one exact setting (a strong sign of overfitting to that window).
- **Check trade count** — with too few trades (say, under ~30), the win
  rate and expectancy numbers aren't statistically meaningful yet, just
  noise.
- **Split into an in-sample and out-of-sample period** — tune weights/
  thresholds on one date range, then check the result holds on a later,
  untouched range before believing it.

Once the technical layer alone shows a real, robust edge across pairs and
time periods, the next step per `MODEL_SPEC.md` §7 is testing whether adding
the PESTLE score actually improves on that — not just adds noise.
