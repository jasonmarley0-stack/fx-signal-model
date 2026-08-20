# Wiring fx-signal-model up to your real Signal Engine

This does **not** modify anything in `~/Documents/Signal-engine` — every step
below only calls its existing REST API from the outside, the same way any
other client would. See `PESTLE_SIGNAL_ENGINE_INTEGRATION.md` for why no code
change is needed there.

## 1. Start Signal Engine

In its own terminal, from `~/Documents/Signal-engine`:

```bash
source .venv/bin/activate   # or however you activate it
uvicorn app.main:app --reload
```

Leave that running. In a second terminal, from `~/fx-signal-model`, confirm
it's reachable:

```bash
curl http://127.0.0.1:8000/health
```

## 2. Seed the 8 currencies and PESTLE SignalTypes

```bash
python3 setup/seed_signal_engine.py
```

This POSTs to `/companies` and `/signal-types` only — no schema or code
changes. Safe to re-run; duplicates are reported, not errors.

## 3. Post real sample evidence

```bash
python3 setup/post_sample_evidence.py
```

This posts the one genuinely-sourced, verified sample event currently in
`setup/seed_sample_evidence.json` — the ECB's 25bp rate hike on 11 June 2026
(source: Euronews) — as a published Signal against the EUR Company, so the
pipeline can be tested against real data rather than fabricated fixtures.

**Before trusting this step**: the exact field names Signal Engine's
`/signals` (or `/evidence`) POST endpoint expects weren't confirmed from this
session — only `app/models/signal_type.py` was available to read, not the
`Signal` model itself. Open `http://127.0.0.1:8000/docs` once Signal Engine
is running, check the real schema for creating evidence, and adjust
`build_payload()` in `setup/post_sample_evidence.py` if the field names
don't match. The script prints the raw response body on failure, which will
usually say exactly what's wrong.

Only one event is seeded right now. Real BoE and Fed decisions in
July 2026 were both **holds with hawkish dissent** — they don't cleanly map
to the current hike/cut-only SignalTypes, and both the US and UK's most
recent CPI prints came in exactly in line with forecast, not clearly above or
below it. Rather than force-fit ambiguous real events into the wrong
SignalType, those were left out. Worth raising with Jase: should
`RATE_HIKE`/`RATE_CUT` become three-way (`hike` / `hold-hawkish` /
`hold-dovish` / `cut`), or is a hold just excluded from PESTLE scoring
entirely?

## 4. Point fx-signal-model at the live instance

```bash
export SIGNAL_ENGINE_BASE_URL=http://127.0.0.1:8000
```

Without this set, `signal_engine_client.py` stays in mock mode (bundled
fixtures) — nothing breaks, it just won't see real data.

## 5. Test the real PESTLE score

```bash
python3 -c "
from src.pestle.pestle_scorer import currency_pestle_score
from src.pestle.signal_engine_client import SignalEngineClient
client = SignalEngineClient()
print('mock mode:', client.mock)
print('EUR PESTLE score:', currency_pestle_score(client, 'EUR'))
"
```

With `SIGNAL_ENGINE_BASE_URL` set and the ECB hike seeded, EUR's economic
category should show a positive contribution from `RATE_HIKE`. If it doesn't,
check step 3's caveat first — a failed POST there means there's nothing for
the scorer to find.

## 6. Combined signal test

Once that works, `src/dashboard_data.py` and `src/combiner.py` will pick up
the real score automatically (same `SIGNAL_ENGINE_BASE_URL` env var) — run
`python3 run_demo.py` to see a full dashboard payload using the live PESTLE
layer instead of mock fixtures.
