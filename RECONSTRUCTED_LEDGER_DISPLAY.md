# Display recovered research outcomes — 29 October 2026

Display-only change on top of the deployed `ef2b49c` file tree. It does not
change scoring, the publisher, the observer, source hashes or raw snapshots.

## Result

The existing v2 ledger shows matched supplementary outcomes with exit time,
price and R, explicitly labelled **Reconstructed**. Original live outcomes
remain in expandable Details. Existing completed live results take precedence.
Unknown EURUSD stays **Unresolved — evidence gap** unless the audit itself
later determines an outcome. No results are hardcoded into the application.

Performance tiles are explicitly **Live Only**. A separate shown-row summary
explains how many ledger outcomes are determined/reconstructed/not determined.
The repeated generic disclaimer moves under Method & limitations; per-row
caveats remain under Details. This is not a blended live performance metric.

## Inputs and safety

The new authenticated `/research/ledger_v2` route reads only:

- `research_snapshots/research_health_v2.json`
- `research_snapshots/research_kpi_v2.json`
- `research_supplementary/research_supplementary_evidence.json`

It adds display fields to an in-memory COPY; `/research/kpi_v2` and all on-disk
files remain unchanged. Initial rendering, manual refresh and automatic refresh
use the display-aware payload. No scoring, broker GET, raw-log scan or service
operation is performed by an HTTP handler.

Reconstructions require exact run/source/contract identity, decision identity,
pair and entry-time match; a single unambiguous record; finite R and positive
exit price; and valid entry/exit/as-of timestamps. Missing/foreign/invalid audit
data falls back to the original live result. It never substitutes a different
trade. Reasons are HTML-escaped; embedded v2 JSON is script-tag safe.

## Tests and deployment

`python -m pytest -q tests/test_reconstructed_ledger_display.py tests/test_dashboard_research_view.py`
passes **30 checks**, including execution of the actual generated JavaScript
in Node for initial, manual and automatic refresh (DOM harness, not a real
browser). Existing tests verify original API/files remain unchanged. The full
main-based regression group also runs before packaging.

In the authorised host-connected session:

1. Apply this patch to a clean checkout at `ef2b49c` (not a summary document).
   Review/test, then merge through main; never switch the shared host checkout
   to an experimental branch.
2. Confirm the actual supplementary JSON exists at the input path above and
   belongs to the current observer run. If another output directory was used,
   resolve that explicitly; do not silently attach a foreign audit.
3. Back up and deploy **only `dashboard_server.py`**, then restart only
   `dashboard-server.service`. No observer/publisher restart or rescore.
4. In the authenticated browser verify USDJPY's stop/target rows now show their
   audit exit/R plus Reconstructed, USDCAD remains the original live time exit,
   and EURUSD remains unresolved. Confirm Details, manual refresh, automatic
   refresh and mobile layout. Check `/research/kpi_v2` is still unchanged.

Rollback: restore the previous dashboard file and restart only the dashboard.
No production deployment or real-browser acceptance was performed from this
environment; GitHub writes and droplet SSH remain unavailable here.
