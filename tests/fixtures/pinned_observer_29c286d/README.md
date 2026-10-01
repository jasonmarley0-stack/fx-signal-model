Read-only copy of `research/prospective_baseline/` and the `src/` modules
it imports, exactly as committed at `29c286d` on
`research/prospective-baseline-observation` — the commit the real Stage 2
observer is pinned at on the droplet
(`/root/fx-signal-model-stage2-observer`).

This exists ONLY so `tests/test_research_snapshot_publisher.py` can point
`research_snapshot_publisher.py` at a real, self-contained copy of the
pinned scorer/report code without depending on that commit being reachable
from this branch's git history (this branch is based on `origin/main`, an
unrelated line of history) or on network/droplet access.

Never imported by `dashboard_server.py` or `research_snapshot_publisher.py`
in production — production always points at the real, live observer
checkout path. Do not edit these files to "fix" something found while
working on the dashboard; if the pinned observer code itself needs a
change, that happens on `research/prospective-baseline-observation`, not
here.
