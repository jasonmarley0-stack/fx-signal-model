# v2 activation handoff

**Not run by this iteration.** Code, tests, and this document are prepared
for Codex's independent review before any activation step below is taken.
No deployment, no observer restart, no orders. The v1 observer
(`/root/fx-signal-model-stage2-observer`, pinned `29c286d`, run
`885c5e3ef4b24446a1be3fc05b8f8577`) keeps collecting, untouched, throughout
and after this iteration.

## Isolation from v1 — by construction, not convention

- **Separate checkout.** v2 activates as its own clone, e.g.
  `/root/fx-signal-model-stage2-observer-v2`, pinned at this branch's
  reviewed commit — never a branch switch inside v1's checkout.
- **Separate package path.** v1's code lives at
  `research/prospective_baseline/`; v2's at
  `research/prospective_baseline_v2/`. Different directory name, so a v2
  checkout cannot be mistaken for a v1 one by path alone.
- **Separate run identity.** v2's `run_identity.py` has its own
  `TRACKED_FILES` (pointing at v2's own files) and writes its own
  `run_manifest.json` into v2's own `logs/` directory. A v1 manifest and a
  v2 manifest can never collide — different files, different hashes,
  different `contract_version` field.
- **Separate logs.** `research/prospective_baseline_v2/logs/` — v1's
  `decisions_log.jsonl`/`quotes_log.jsonl`/`health_log.jsonl` are never
  read, written, or appended to by any v2 code path.
- **Separate dashboard snapshots.** `research_snapshot_publisher.py
  --contract-version v2` writes `research_health_v2.json`/
  `research_kpi_v2.json` — never the unsuffixed v1 files (see
  `test_v2_publisher_path_writes_separate_suffixed_files_never_touching_v1`).
  The dashboard renders them as their own, clearly labeled "Research v2 —
  New Measurement Contract" section, never merged into v1's figures.

## Pre-activation checklist (for whoever activates, after review)

1. Clone this branch to the new isolated checkout; `cd` into it; create
   `.venv`; install `requirements.txt` (same as v1's own setup).
2. Copy `setup/oanda.env` from the existing v1 checkout (or wherever it's
   kept) into the new checkout's `setup/` — same practice-only credential,
   never re-typed, never printed.
3. Practice-only smoke check (same pattern as v1's, pointed at v2):
   ```bash
   cd /root/fx-signal-model-stage2-observer-v2
   set -a; source setup/oanda.env; set +a
   .venv/bin/python3 -c "
   import sys; sys.path.insert(0, 'research/prospective_baseline_v2')
   from run_identity import enforce_practice_environment
   enforce_practice_environment()
   print('v2 practice guard OK')
   "
   ```
4. Pricing + completed-H4 smoke checks — identical commands to v1's
   README, with `research/prospective_baseline_v2` substituted for
   `research/prospective_baseline`.
5. Install a new systemd unit, e.g.
   `prospective-baseline-observer-v2.service`
   (`ExecStart=.../prospective_baseline_v2/observe.py`,
   `WorkingDirectory=` the new checkout) — a different unit name from
   v1's `prospective-baseline-observer.service`, so `systemctl restart`
   on one can never touch the other.
6. `systemctl daemon-reload && systemctl enable --now
   prospective-baseline-observer-v2.service`. First output line:
   `v2 run identity: run_id=... source_hash=...` — a fresh run_id,
   confirming this is a genuinely new run, not a continuation of v1's.
7. Install two new publisher timers (copy the pattern of
   `setup/research-health-publisher.*`/`research-kpi-publisher.*`,
   pointed at the v2 checkout with `--contract-version v2`).
8. `systemctl restart dashboard-server.service` (dashboard-only, same
   discipline as the v1 Research-view deployment) so it starts reading
   the new v2 snapshot files too.
9. Verify: the dashboard's "Research v2" section shows `state: ok`, a
   fresh `run_id`, and `service: active` — all independent of, and
   simultaneous with, v1's own unaffected section above it.

## Rollback

Stop and disable the two new v2 systemd units
(`prospective-baseline-observer-v2.service`, the two v2 publisher timers).
Nothing else needs to change: v1 was never touched, so there is nothing
to restore on the v1 side. The v2 checkout, logs, and manifest can be
left in place (inert) or removed entirely — neither affects v1.

## What is preserved, untouched, forever

v1's `research/prospective_baseline/logs/` (decisions, quotes, health,
manifest) and every KPI figure already published from run
`885c5e3ef4b24446a1be3fc05b8f8577` remain exactly as recorded. This
iteration never writes to them. The replay in `replay_v1_under_v2.py`
reads v1's recorded decisions and a bounded quote extract for comparison
purposes only, and writes its own output to
`research/prospective_baseline_v2/output/replay_v1_under_v2_diagnostic.json`
— never back into v1's own logs or score files.
