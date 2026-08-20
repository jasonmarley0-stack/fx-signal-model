# (Deprecated) Signal Engine category enum patch

**This is no longer needed and should not be applied.** An earlier version of
this repo proposed adding five members to Signal Engine's `SignalCategory`
enum in `app/models/signal_type.py`. Per Jase's instruction, Signal Engine's
own codebase stays untouched — instead, PESTLE SignalTypes are seeded using
Signal Engine's *existing* category values, with true PESTLE categorisation
handled entirely on the fx-signal-model side. See
`PESTLE_SIGNAL_ENGINE_INTEGRATION.md` §3 for the current approach, and
`setup/seed_pestle_signal_types.json`'s `pestle_category` field /
`src/pestle/signal_engine_client.py`'s `PESTLE_SIGNAL_POLARITY` table for
where the real classification lives.

This file is kept only so nothing references a missing file; safe to delete.
