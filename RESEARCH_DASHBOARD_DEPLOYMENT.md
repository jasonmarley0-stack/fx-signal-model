# Deploying the Research dashboard view (revised)

Supersedes the deployment steps given in chat when this branch was first
pushed, which suggested `git checkout research/dashboard-observation`
directly on the droplet. **That is wrong and must not be done** — see
"Why not `git checkout` the research branch" below. This document is the
one to follow.

Not run by this commit. This is a documentation-only correction; no
deployment happens here.

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

Deploy by merging the reviewed, tested commit into `main` — never by
checking out the research branch in place:

```bash
# wherever you do the actual merge (not necessarily on the droplet) --
# fast-forward research/dashboard-observation's reviewed commit into main:
git checkout main
git merge --ff-only research/dashboard-observation   # or review+merge the PR on GitHub
git push origin main
```

Record which commit this was (e.g. `git log -1 --format=%H main` after
the merge) — that SHA is "the reviewed dashboard/publisher release" for
rollback purposes below.

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

Two options, in order of preference:

**1. Revert on `main` and re-pull (clean history, preferred):**
```bash
git revert <merge-commit-sha>        # wherever you merge from, not necessarily the droplet
git push origin main
# on the droplet:
cd /root/fx-signal-model && git pull --ff-only origin main
systemctl restart dashboard-server.service   # dashboard-only, as above
systemctl disable --now research-health-publisher.timer research-kpi-publisher.timer   # optional: stop publishing if rolling back fully
```

**2. Fast file-level rollback (if you need the dashboard back immediately and can clean up history later):**
```bash
cd /root/fx-signal-model
git log --oneline -- dashboard_server.py | head -5    # find the pre-release SHA
git checkout <pre-release-sha> -- dashboard_server.py
systemctl restart dashboard-server.service             # dashboard-only
# commit this restoration properly on main afterward -- a working-tree-only
# revert like this must not become the permanent record.
```

Either way: `live-scanner.service`, every other droplet service, and the
Stage 2 observer checkout are never part of a Research-dashboard
rollback — there is nothing there to roll back.

## Review cadence

Pin this deployment's SHA somewhere you'll see again (a comment in your
own notes, a tag, whatever you already use for the droplet) so a future
"what's actually running" question has one answer, the same way
`run_manifest.json` answers it for the observer.
