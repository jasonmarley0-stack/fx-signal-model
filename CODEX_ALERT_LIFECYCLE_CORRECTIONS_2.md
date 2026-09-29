# Second correction pass on the alert lifecycle branch, per Codex's review of `03768e4`

Branch: `alert-lifecycle-prospective-scoring`. Same constraints throughout: no merge, no deploy, no service restart, no orders, no strategy/threshold/stop/target changes.

## 1. Dashboard live alert cards updated to the new feed schema

`dashboard_server.py`'s `alert_card_html()` was still written for the *old* `push_alert()` shape (`stop_loss_range`/`take_profit_range`, `window`, branching on `direction == "no_trade"` for a stand-down card). Rewritten to branch on `event_type` first:

- **`issued`/`revised`** cards now show the actual single-value `stop`/`target` and `entry_expiry_utc` (as "Enter by …") the new schema carries. A `revised` card additionally shows an "Updated" badge, a "Revises alert X" note, and — this is requirement 4's disclosure, now visible in the actual rendered card, not just present as data — the `existing_position_guidance` text stating the *original* alert's stop/target still apply to anyone already in that trade.
- **`cancelled`** cards are structurally distinct: no `entry mono` field is rendered at all (there is no entry price on a cancellation event), no `long`/`short` badge is ever emitted regardless of what direction the cancelled alert carried, and the card explicitly says "Cancelled" plus "No entry price applies to this update." Verified directly on a reversal fixture, where the cancellation card's own `direction` field is `"long"` (the direction being cancelled) — confirmed the rendered HTML contains no `badge long`/`badge short` anywhere in that card.
- **Browser notification wording** (the `pollAlerts()` JS in `render_page`) previously built every notification title from `a.direction.toUpperCase()` — for a cancellation whose `direction` field carries the *cancelled* alert's direction, this produced a notification that read like a fresh instruction (e.g. "EURUSD — SHORT" for a cancellation of a short). Fixed to branch on `event_type`: `"EURUSD — Cancelled"` for cancellations, `"EURUSD — Updated LONG"` for revisions, the original direction-based title only for a genuine new issuance.

Rendered examples of all four cases (issued, revised, cancelled, and the new post-reversal issuance) are attached separately and reproduced in the report below.

## 2. Fixed: entry candle was skipped from its own stop/target scan

Real bug, exactly as Codex reproduced. `score_version()`'s post-entry scan started at `candles.index > entry_time` — **strictly excluding** the very candle that confirmed the entry. Since entry happens at that candle's *open*, the same candle's high/low can still reach stop, target, or both before the candle closes; excluding it meant that if the entry candle itself was ambiguous (crossed both levels) or hit the stop, and a *later* candle then reached target cleanly, the scorer reported that later, spurious target hit as the outcome — Codex's repro: entry candle crosses both stop and target, a later candle reaches target, old code reports ~+1R.

Fixed by changing the filter to `candles.index >= entry_time` (`src/alert_scorer.py`), so the entry candle is included in the same stop/target/ambiguity check as every other post-entry candle. A bar that crosses both levels — including the entry candle itself — still correctly reports `ambiguous_intrabar_exit`, not a win.

**Regression test added:** `tests/test_alert_scorer_edge_cases.py::test_entry_candle_itself_crossing_both_levels_is_not_skipped` — builds exactly Codex's scenario (entry candle's open confirms entry and its own high/low crosses both levels; a later candle reaches target cleanly) and asserts the result is `ambiguous_intrabar_exit` with `r_multiple is None`, not `targeted`/`+1R`.

## 3. `feed_publish_result.status` renamed, not just documented

Renamed the value from `"delivered"` to `FEED_PUBLISH_STATUS_WRITTEN = "written_to_feed_file"` (`src/alert_lifecycle.py`), used consistently in `src/alert_feed_publisher.py` and the tests. Chose rename over documentation-only because "delivered" is the kind of word that gets copy-pasted into a future dashboard or subscriber-facing summary without anyone re-reading its docstring; a name that says what it actually means is harder to misuse.

Docstrings on the constant, `record_feed_publish_result()`, and `unpublished_events()` all now state explicitly: this means the entry was successfully appended to `alerts.json`, nothing more — not that a browser received it, not that a `Notification` fired, not that any subscriber saw it. None of that is observed anywhere in this system, and nothing in the code claims otherwise. `NOTIFICATION_DELAY_ASSUMPTION_SECONDS = 0` in `alert_scorer.py` (unchanged) remains the explicit, separate marker that notification delay is an assumption for scoring purposes, not a measurement.

## Tests

New: `tests/test_dashboard_rendering.py` (5 tests) — renders real feed entries produced by `alert_feed_publisher.record_and_publish` (not hand-built fixture dicts) through the actual `alert_card_html()`/`render_page()` functions: issued card shows entry/stop/target/expiry; revised card shows the Updated badge, the new levels, and the original levels inside the guidance text; cancelled card has no entry-price field and no direction badge; a reversal's two feed entries render as two visually/textually distinct cards (cancellation vs. new issuance); the notification JS text contains the `event_type`-aware branches.

Extended: `tests/test_alert_scorer_edge_cases.py` (+1 test, the regression test above).

Full suite run (28 test functions, 6 files), all passing:
```
tests/test_sessions_and_orb.py          2/2  (pre-existing, unaffected)
tests/test_alert_lifecycle.py           9/9
tests/test_alert_scorer_edge_cases.py   7/7  (was 6, +1 regression test)
tests/test_alert_feed_integration.py    6/6
tests/test_dashboard_rendering.py       5/5  (new)
```

**Local environment note:** running `tests/test_dashboard_rendering.py` required installing `eval_type_backport` into the local `.venv` — `dashboard_server.py`'s `X | None` type hints need Python 3.10+ or that backport package under pydantic's own type-resolution machinery on 3.9. This is a local dev-environment gap (same category as the `oandapyV20`/`requests` install from the first correction pass), not a repository code change, and not something introduced by this branch.

## Remaining limitations, stated plainly

- Notification wording is verified by asserting the JS source text contains the right conditional branches (no JS runtime in this test suite) — not executed in an actual browser.
- The existing-position guidance text is plain English generated server-side; it has not been reviewed for tone/length against what a real notification payload or push-service character limit would allow.
- Still nothing here has touched real OANDA data, a real browser, or been deployed anywhere.
