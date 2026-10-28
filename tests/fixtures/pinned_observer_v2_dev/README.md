A minimal copy of `research/prospective_baseline_v2/{contract,score,report,
run_identity}.py`, used only by
`tests/test_research_snapshot_publisher.py`'s v2-path test to exercise
`research_snapshot_publisher.py --contract-version v2` against a
self-contained checkout shape, without needing `observe.py`/
`quote_client.py` (not required for scoring/publishing) or the real `src/`
strategy modules (this fixture's `run_identity.py` hashes them as
`<MISSING>`, which is fine — the test only needs a stable, internally
consistent hash, not the real production one).

Unlike `pinned_observer_29c286d/` (an exact, hash-verified copy of a real
pinned commit), this is a development-convenience copy of the in-progress
v2 source, not a verified production pin. If v2's core files change,
re-copy them here:

```bash
cp research/prospective_baseline_v2/{contract,score,report,run_identity}.py \
   tests/fixtures/pinned_observer_v2_dev/research/prospective_baseline_v2/
```
