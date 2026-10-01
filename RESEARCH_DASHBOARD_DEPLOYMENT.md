# Deploying the Research dashboard view (revised)

Supersedes the deployment steps given in chat when this branch was first
pushed, which suggested `git checkout research/dashboard-observation`
directly on the droplet. **That is wrong and must not be done** — see
"The constraint this procedure exists to satisfy" below. This document is
the one to follow.

## Pre-release state (recorded before this deployment)

- **Pre-release SHA (origin/main tip immediately before this deployment):**
  `43b78cc14b8a2ab1c37928004ce250a1ebd76af3`
  (`"Automated pending-evidence snapshot (94 unreviewed)"` — the automated
  evidence/status pipeline moves `main` forward on its own schedule;
  this is simply whatever its tip was at merge time, not a meaningful
  commit in its own right. Recorded here only as the rollback anchor.)
- **Reviewed release being deployed:** both `research/dashboard-observation`
  commits — `fc69c26` (the Research view + publisher) and `3d70952` (the
  null/roadmap/run-id correction pass) — merged into `main` as a single
  merge commit. `main` had moved since the branch was cut (unrelated
  automated evidence/status commits only — confirmed no overlap with any
  file this release touches), so the merge is a real merge commit, not a
  fast-forward; the rollback steps below account for that.

## The constraint this procedure exists to satisfy

`/root/fx-signal-model` on the droplet is **one shared git checkout**
behind multiple independent systemd services — `live-scanner.service`
(runs `live_scanner.py`), `dashboard-server.service` (runs
`dashboard_server.py`), `performance-scorer.service`, and others, all with
`WorkingDirectory=/root/fx-signal-model`. It is currently on `main`. A
`git checkout <other-branch>` there would switch **every one of those
services'** code at once, not just the dashboard — exactly the "switching
its active checkout wholesale" this procedure avoids.

The Stage 2 observer at `/root/fx-signal-model-stage2-observer` is a
**completely separate checkout**, pinned at commit `29c286d`, running its
own `prospective-baseline-observer.service`. Nothing in this procedure
ever touches it: no edits to its tracked files, no manifest writes, no
restart, no checkout switch. The research-health-publisher and
research-kpi-publisher units (step 3 below) only ever *read* it.

## The reviewed release

Deploy by merging the reviewed, tested branch into `main` — never by
checking out the research branch in place. `main` has moved since the
branch was cut, so this is a real merge commit, not a fast-forward:

```bash
# wherever you do the actual merge (not necessarily on the droplet) --
git fetch origin
git checkout main
git merge --no-ff origin/research/dashboard-observation   # or review+merge the PR on GitHub; brings in BOTH fc69c26 and 3d70952
git push origin main
```

Record the resulting merge commit's SHA (e.g. `git log -1 --format=%H main`
after pushing) — that is "the reviewed dashboard/publisher release" for
rollback purposes below. Together with the pre-release SHA recorded above,
that's the full before/after pair this deployment is bounded by.

## Deployment steps (dashboard-only — never touches live-scanner or the observer)

```bash
# on the droplet, in the SHARED checkout -- stays on main throughout, never `git checkout` to another branch:
cd /root/fx-signal-model
git fetch origin
git log -1 --format=%H origin/main                       # note this SHA -- the release being deployed
git pull --ff-only origin main                            # fast-forward only; refuses (does not silently merge/rebase) if main has diverged

# install the two NEW publisher timers (these are additions, not a restart of anything existing):
cp setup/research-health-publisher.service setup/research-health-publisher.timer \
   setup/research-kpi-publisher.service setup/research-kpi-publisher.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now research-health-publisher.timer research-kpi-publisher.timer

# confirm snapshots start appearing (read-only check):
ls -la /root/fx-signal-model/research_snapshots/
cat /root/fx-signal-model/research_snapshots/research_health.json | head -5

# dashboard-only restart -- the ONLY existing service this deployment restarts:
systemctl restart dashboard-server.service
systemctl status dashboard-server.service --no-pager

# confirm the OTHER services were never touched (expect: same PID/start time as before this deployment):
systemctl status live-scanner.timer --no-pager
systemctl status performance-scorer.timer --no-pager
```

If `git pull --ff-only` refuses (main has local, uncommitted, or diverged
changes on the droplet), stop and investigate — do not force it with
`--rebase`, `reset --hard`, or a branch checkout. That divergence is real
state to understand, not an obstacle to bulldoze.

## Rollback

Both options below undo **the entire release** (both `fc69c26` and
`3d70952` together) in one step — `3d70952`'s fixes are layered on top of
`fc69c26`'s changes within the same files, so restoring those files to
their pre-release content (the pre-release SHA recorded above) always
covers both commits at once; there is no scenario where you'd want one
without the other.

**1. Revert on `main` and re-pull (clean history, preferred):**
```bash
# the deployment merge is a MERGE commit (main had moved since the branch
# was cut) -- a plain `git revert` refuses on a merge commit without
# being told which parent is "mainline"; -m 1 is that flag:
git revert -m 1 <merge-commit-sha>        # wherever you merge from, not necessarily the droplet
git push origin main
# on the droplet:
cd /root/fx-signal-model && git pull --ff-only origin main
systemctl restart dashboard-server.service   # dashboard-only, as above
systemctl disable --now research-health-publisher.timer research-kpi-publisher.timer   # stop publishing if rolling back fully
```

**2. Fast file-level rollback (if you need the dashboard back immediately and can clean up history later):**
```bash
cd /root/fx-signal-model
# restore dashboard_server.py to its exact pre-release content -- this
# alone removes the Research view/routes entirely (both commits' worth),
# since 3d70952's fixes only exist within the Research-view code fc69c26
# added:
git checkout 43b78cc14b8a2ab1c37928004ce250a1ebd76af3 -- dashboard_server.py
systemctl restart dashboard-server.service             # dashboard-only

# research_snapshot_publisher.py did not exist before this release, so
# there's nothing to "restore" it to -- stopping the timers (not deleting
# the file) is the correct rollback for it: an unreferenced script that
# nothing calls is inert, and deleting it is unnecessary churn for an
# emergency rollback.
systemctl disable --now research-health-publisher.timer research-kpi-publisher.timer

# commit this restoration properly on main afterward (e.g. `git revert -m 1`
# as in option 1) -- a working-tree-only revert like this must not become
# the permanent record.
```

Either way: `live-scanner.service`, every other droplet service, and the
Stage 2 observer checkout are never part of a Research-dashboard
rollback — there is nothing there to roll back.

## Review cadence

Pin this deployment's SHA somewhere you'll see again (a comment in your
own notes, a tag, whatever you already use for the droplet) so a future
"what's actually running" question has one answer, the same way
`run_manifest.json` answers it for the observer.
