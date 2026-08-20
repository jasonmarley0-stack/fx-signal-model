# Setting up the intraday "hey Jase, look at this" monitor

This runs entirely on your Mac via `launchd` (macOS's built-in scheduler) —
not via a Claude-scheduled task, because Signal Engine and your Dukascopy
access only exist locally. Every run recomputes real signals for the default
7 pairs and only pops a native notification when something genuinely new
crosses the bar (see `monitor_signals.py`'s docstring for the exact trigger
logic: high-confidence combined signal, or fresh PESTLE evidence in the last
3 hours).

## 1. Prerequisite: Signal Engine must be running

The monitor needs `SIGNAL_ENGINE_BASE_URL` set and Signal Engine reachable —
same as everything else we've tested. If Signal Engine isn't running when a
scheduled check fires, the script logs that and exits cleanly (no crash, no
false alert).

Easiest: keep `uvicorn app.main:app --reload` running in a terminal you
leave open (or set up its own separate launchd job if you want it to survive
reboots — ask if you want that written too).

## 2. Test the monitor manually first

```bash
cd ~/fx-signal-model
export SIGNAL_ENGINE_BASE_URL=http://127.0.0.1:8000
python3 monitor_signals.py
```

You should see either "nothing new to flag" or an `ALERT:` line plus a
native notification. Run it twice in a row — the second run should NOT
re-alert on the same unchanged state (that's `.monitor_state.json` working).

## 3. Install the launchd job

```bash
mkdir -p ~/Library/LaunchAgents
cp setup/com.fx-signal-model.monitor.plist ~/Library/LaunchAgents/
```

**Edit the copied plist first** — it currently hardcodes
`/Users/jasonmarley/fx-signal-model` for both the Python interpreter path
and the working directory. Open
`~/Library/LaunchAgents/com.fx-signal-model.monitor.plist` and confirm those
paths match where your `.venv` and repo actually live (they should already
be correct, but check after any folder moves).

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.fx-signal-model.monitor.plist
```

It'll now run every 30 minutes, all day, every day — including outside
trading hours and weekends. That's a deliberate simplification for v1: the
script itself doesn't check session times before running (it's cheap enough
to just check and find nothing), but if you want it to only run during
London/NY hours to save the Dukascopy calls, say so and I'll add that guard
to `monitor_signals.py` rather than fighting `launchd`'s calendar syntax.

Check it's loaded:

```bash
launchctl list | grep fx-signal-model
```

## 4. Stopping it

```bash
launchctl unload ~/Library/LaunchAgents/com.fx-signal-model.monitor.plist
```

## 5. What this does NOT do yet

- It refreshes `live_morning_dashboard.html` locally on an alert, but does
  **not** push that update to the persisted Cowork artifact — that requires
  a Claude session with the device bridge connected. After an alert, if you
  want the artifact itself updated, tell me in chat and I'll pull the
  refreshed file and push it.
- It doesn't yet fold in the economic calendar (upcoming events like a UK
  budget) — that's still pending the Signal Engine connector work we
  discussed, since it depends on a live web fetch only a Claude session can
  do reliably.
- Logs go to `monitor.log` / `monitor.error.log` in the repo folder if you
  want to check what happened between alerts.
