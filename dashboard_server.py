"""Signal IQ's live dashboard: reads live_scan.json (written by
streaming_scanner.py roughly every 30s), alerts.json (last ~50 fired signal
transitions), and performance.json (written by performance_scorer.py
hourly) and renders them as a password-protected web page. Deliberately
does NOT call OANDA, Signal Engine, or performance_scorer itself — it only
ever reads the snapshot files those write, so this page can be slow,
crash, or get hammered by refreshes without ever affecting anything
upstream.

Two views, switched client-side (no full page reload, so in-page state —
alert tracking, notification permission — survives switching):
  - Live: a feed of fired-signal cards (full technical + PESTLE evidence
    breakdown per card — see streaming_scanner.py's push_alert, which
    persists that breakdown specifically so this page can show it) above
    the market table. Polls /alerts every 5s for notification purposes
    (pops a browser Notification, plays a beep, flashes the row); when
    that poll finds something genuinely new it also refreshes the feed
    via /feed and the table via /table (partials, not a full reload, so
    in-page JS state survives).
  - Performance: signal-outcome and directional-accuracy track record from
    performance.json, with a 7D/30D/All range toggle. Rendered once at
    page load — the data behind it only changes hourly, so unlike Live
    there's no polling; reload the page for fresh numbers.

Auth: HTTP Basic, credentials from setup/dashboard.env (DASHBOARD_USER /
DASHBOARD_PASSWORD) — see setup/dashboard-server.service. Not meant to
replace a real login system; it's a lightweight gate so the page isn't
wide open to the entire internet on your droplet's public IP.

Usage:
    python3 dashboard_server.py           # serves on :8080
"""
from __future__ import annotations
import html
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Depends, Header, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

LIVE_SCAN_PATH = Path(__file__).parent / "live_scan.json"
ALERTS_PATH = Path(__file__).parent / "alerts.json"
PERFORMANCE_PATH = Path(__file__).parent / "performance.json"

# Research (Stage 2 prospective observation) view: reads ONLY these two
# pre-computed snapshot files, written by the separate
# research_snapshot_publisher.py (see that file's own docstring). This
# process never calls OANDA, never scores anything, never scans the
# observer's raw logs, and never shells out to systemctl -- by design,
# so a slow/broken dashboard request can never affect the frozen observer
# checkout it reports on.
RESEARCH_HEALTH_PATH = Path(__file__).parent / "research_snapshots" / "research_health.json"
RESEARCH_KPI_PATH = Path(__file__).parent / "research_snapshots" / "research_kpi.json"
RESEARCH_HEALTH_STALE_AFTER_SECONDS = 300    # health publisher runs ~every 60s
RESEARCH_KPI_STALE_AFTER_SECONDS = 3600      # kpi publisher runs ~every 15-30 min

# v2 measurement contract -- COMPLETELY SEPARATE snapshot files, own run
# identity, own observer checkout. See research/prospective_baseline_v2/
# MEASUREMENT_CONTRACT.md. Never read, written, or rendered together with
# the v1 paths above as if they were one figure -- the Research view
# renders them as two clearly labeled, independent sections.
RESEARCH_HEALTH_V2_PATH = Path(__file__).parent / "research_snapshots" / "research_health_v2.json"
RESEARCH_KPI_V2_PATH = Path(__file__).parent / "research_snapshots" / "research_kpi_v2.json"

app = FastAPI()
security = HTTPBasic()
security_optional = HTTPBasic(auto_error=False)  # for routes that also accept the monitor API key


def _basic_auth_ok(credentials: HTTPBasicCredentials | None) -> bool:
    user = os.environ.get("DASHBOARD_USER", "")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not credentials or not user or not password:
        return False
    return secrets.compare_digest(credentials.username, user) and secrets.compare_digest(credentials.password, password)


def check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    if not _basic_auth_ok(credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def check_auth_or_monitor_key(
    credentials: HTTPBasicCredentials | None = Depends(security_optional),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """/api/health and /api/performance accept either the human dashboard
    login (Basic Auth, browser use) or a separate MONITOR_API_KEY via
    header — a scheduled automation gets its own narrow, revocable
    credential instead of reusing the person's own login."""
    monitor_key = os.environ.get("MONITOR_API_KEY", "")
    if monitor_key and x_api_key and secrets.compare_digest(x_api_key, monitor_key):
        return
    if _basic_auth_ok(credentials):
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Basic"},
    )


def fmt_price(v: float | None) -> str:
    return f"{v:.5f}" if isinstance(v, (int, float)) else "—"


ARROW_GLYPH = {"up": ("▲", "arrow-up"), "down": ("▼", "arrow-down"), "flat": ("—", "arrow-flat")}


def sparkline_svg(values: list[float] | None) -> str:
    """A small inline trendline for the market table — ~24h of M30 closes
    (see streaming_scanner.py's SPARKLINE_BARS). Green/red purely by net
    direction over the window shown, not tied to the signal direction —
    this is a price trend at a glance, not a restatement of the badge."""
    if not values or len(values) < 2:
        return '<span class="sparkline-empty">—</span>'
    w, h = 72, 24
    vmin, vmax = min(values), max(values)
    span = (vmax - vmin) or 1
    xs = lambda i: i / (len(values) - 1) * w  # noqa: E731
    ys = lambda v: h - ((v - vmin) / span) * h  # noqa: E731
    points = " ".join(f"{xs(i):.1f},{ys(v):.1f}" for i, v in enumerate(values))
    color = "var(--long)" if values[-1] >= values[0] else "var(--short)"
    return (f'<svg class="sparkline" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.6" '
            f'stroke-linejoin="round" stroke-linecap="round" /></svg>')


def row_html(row: dict) -> str:
    pair = row["pair"]
    if "error" in row:
        return f"""
        <tr class="err-row" data-pair="{pair}">
          <td>{pair}</td><td colspan="7" class="err">{row['error']}</td>
        </tr>"""

    direction = row["direction"]
    dir_cls = {"long": "long", "short": "short", "no_trade": "neutral"}[direction]
    dir_label = {"long": "LONG", "short": "SHORT", "no_trade": "NO TRADE"}[direction]
    sl = row["stop_loss_range"]
    tp = row["take_profit_range"]
    window_str = ""
    if row.get("window"):
        w = row["window"]
        window_str = f"<div class='window'>{w.get('session', '')}: {w.get('generated_at_gmt_str', '')}–{w.get('valid_until_gmt_str', '')}</div>"
    glyph, arrow_cls = ARROW_GLYPH.get(row.get("price_arrow", "flat"), ARROW_GLYPH["flat"])

    return f"""
    <tr data-pair="{pair}">
      <td class="pair">{pair}</td>
      <td class="num">{fmt_price(row['entry'])} <span class="arrow {arrow_cls}">{glyph}</span></td>
      <td>{sparkline_svg(row.get('sparkline'))}</td>
      <td class="num tech-breakdown">
        orb {row['tech']['orb']:+.2f} · trend {row['tech']['trend']:+.2f} · pat {row['tech']['pattern']:+.2f}
        <div class="composite">composite {row['tech']['composite']:+.2f}</div>
      </td>
      <td class="num">{row['pestle_score']:+.2f}</td>
      <td class="num combined">{row['combined_score']:+.2f}</td>
      <td><span class="badge {dir_cls}">{dir_label}</span> <span class="conf">{row['confidence']}</span></td>
      <td class="num sltp">
        SL {fmt_price(min(sl))}–{fmt_price(max(sl))}<br/>
        TP {fmt_price(min(tp))}–{fmt_price(max(tp))}
      </td>
    </tr>
    <tr class="reason-row" data-pair="{pair}"><td></td><td colspan="7" class="reason">{row['reason']}{window_str}</td></tr>"""


def render_table_body(payload: dict | None) -> str:
    """The part of the page that /table refreshes in place: the "last scan"
    subtitle plus the results table. Kept separate from the outer page shell
    so a poll can swap it in without disturbing the page's JS state (alert
    tracking, notification permission, etc.)."""
    if payload is None:
        return '<div class="subtitle">No scan data yet — the scanner hasn\'t completed its first pass. Check back in a few minutes.</div>'

    generated = datetime.fromisoformat(payload["generated_at"])
    age_sec = (datetime.now(timezone.utc) - generated).total_seconds()
    generated_str = f"{generated.strftime('%Y-%m-%d %H:%M:%S UTC')} ({age_sec:.0f}s ago)"
    rows_html = "".join(row_html(r) for r in payload["rows"])
    return f"""
    <div class="subtitle">Last scan: {generated_str}</div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>Pair</th><th>Price</th><th>Trend</th><th>Technical</th><th>PESTLE</th>
          <th>Combined</th><th>Signal</th><th>SL / TP</th>
        </tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>"""


def relative_time(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    try:
        t = datetime.fromisoformat(iso_str)
    except ValueError:
        return ""
    age = (datetime.now(timezone.utc) - t).total_seconds()
    if age < 60:
        return "just now"
    if age < 3600:
        return f"{int(age // 60)}m ago"
    if age < 86400:
        return f"{int(age // 3600)}h ago"
    return f"{int(age // 86400)}d ago"


def bar_row_html(label: str, value: float) -> str:
    """One technical-component bar for an alert card: a bidirectional bar
    from the track's center, matching how combiner.py's components are
    conviction votes in [-1,1], not a magnitude — see the chat discussion
    on what -1..+1 means. Width is purely abs(value), direction is which
    side of center it fills."""
    width_pct = min(abs(value), 1.0) * 50
    cls = "pos" if value >= 0 else "neg"
    return f"""<div class="bar-row"><span class="bar-label">{label}</span>
      <div class="bar-track"><span class="bar-mid"></span><span class="bar-fill {cls}" style="width:{width_pct:.0f}%"></span></div>
      <span class="bar-val mono">{value:+.2f}</span></div>"""


def evidence_html(evidence: list[dict] | None) -> str:
    if not evidence:
        return '<div class="evidence-item">No recent evidence in the scoring window.</div>'
    items = "".join(
        f'<div class="evidence-item">{e["title"]} <span class="src">— {relative_time(e.get("observed_at"))}</span></div>'
        for e in evidence
    )
    return f'<div class="evidence">{items}</div>'


def alert_card_html(alert: dict) -> str:
    """Renders one fired-signal alert with its full technical + PESTLE
    breakdown — the data streaming_scanner.py's push_alert() now persists
    specifically so this card can show *why*, not just the headline
    direction/confidence (see SIGNAL_IQ_GAP_ANALYSIS.md item 1b and
    SIGNAL_DEFINITION_AND_ACCURACY.md on keeping that breakdown visible)."""
    pair = alert["pair"]
    direction = alert["direction"]
    when = relative_time(alert.get("time"))
    alert_id = alert.get("id", "")

    if direction == "no_trade":
        return f"""
        <article class="card dir-flat standdown" data-alert-id="{alert_id}">
          <div class="card-top">
            <div class="card-id"><span class="pair-name">{pair}</span><span class="badge neutral">No trade</span></div>
            <div class="card-meta"><div>{when}</div></div>
          </div>
          <p class="standdown-note">Signal stood down — {alert.get('reason', '')}</p>
        </article>"""

    dir_label = {"long": "Long", "short": "Short"}[direction]
    tech = alert.get("tech") or {}
    pestle = alert.get("pestle") or {}
    base = pestle.get("base", {})
    quote = pestle.get("quote", {})
    sl = alert.get("stop_loss_range") or [alert["entry"], alert["entry"]]
    tp = alert.get("take_profit_range") or [alert["entry"], alert["entry"]]
    window = alert.get("window")
    window_html = ""
    if window:
        window_html = f'<span class="session">{window.get("session", "")}</span> · valid to {window.get("valid_until_gmt_str", "")}'

    return f"""
    <article class="card dir-{direction}" data-alert-id="{alert_id}">
      <div class="card-top">
        <div class="card-id">
          <span class="pair-name">{pair}</span>
          <span class="badge {direction}">{dir_label}</span>
          <span class="badge conf-{alert['confidence']}">{alert['confidence']} confidence</span>
        </div>
        <div class="card-meta">
          <div class="entry mono">{fmt_price(alert.get('entry'))}</div>
          <div>{when}</div>
        </div>
      </div>
      <div class="card-grid">
        <div>
          <div class="card-section-label">Technical · {tech.get('composite', 0):+.2f}</div>
          {bar_row_html('ORB', tech.get('orb', 0))}
          {bar_row_html('Trend', tech.get('trend', 0))}
          {bar_row_html('Pattern', tech.get('pattern', 0))}
        </div>
        <div>
          <div class="card-section-label">PESTLE · {base.get('currency', '')} {base.get('score', 0):+.2f}</div>
          {evidence_html(base.get('evidence'))}
        </div>
        <div>
          <div class="card-section-label">PESTLE · {quote.get('currency', '')} {quote.get('score', 0):+.2f}</div>
          {evidence_html(quote.get('evidence'))}
        </div>
      </div>
      <div class="card-footer">
        <div class="sltp-group">
          <span><span class="k">SL</span> <span class="v mono">{fmt_price(min(sl))}–{fmt_price(max(sl))}</span></span>
          <span><span class="k">TP</span> <span class="v mono">{fmt_price(min(tp))}–{fmt_price(max(tp))}</span></span>
        </div>
        <div class="window-note">{window_html}</div>
      </div>
    </article>"""


def render_feed_body(alerts_payload: dict | None) -> str:
    """Newest-first, capped at 20 shown — alerts.json itself already caps
    at ~50 stored. Separate from render_table_body/render_performance_view
    so /feed can refresh just this block."""
    alerts = (alerts_payload or {}).get("alerts", [])
    if not alerts:
        return '<p class="empty">No signals have fired yet — this fills in the moment a real direction/confidence transition happens.</p>'
    cards = "".join(alert_card_html(a) for a in reversed(alerts[-20:]))
    return f'<div class="feed">{cards}</div>'


def fmt_pct(v: float | None) -> str:
    return f"{v:.0%}" if isinstance(v, (int, float)) else "—"


def fmt_r(v: float | None) -> str:
    return f"{v:+.2f}R" if isinstance(v, (int, float)) else "—"


def render_performance_view(performance: dict | None) -> str:
    """Static at page load — performance.json only changes hourly (see
    performance_scorer.py), so unlike the Live table this doesn't need
    polling. All three range windows are embedded as data and switched
    client-side (renderPerformanceRange in the page script) rather than
    rendered three times server-side."""
    if performance is None:
        return """
        <div class="subtitle">No performance data yet — performance_scorer.py hasn't run yet.</div>
        <p class="empty">Check back once it's had a chance to run (hourly via systemd timer).</p>"""

    all_agg = performance["aggregates"]["all"]
    if all_agg["total_signals"] == 0:
        return """
        <div class="subtitle">No signals scored yet.</div>
        <p class="empty">Nothing has fired since the streaming scanner went live — this fills in
        automatically the moment a real signal fires and enough time passes to score it.</p>"""

    data_json = json.dumps({"aggregates": performance["aggregates"], "signals": performance.get("signals", [])})
    return f"""
    <div class="range-toggle" id="perf-range-toggle">
      <button data-range="7d">7D</button>
      <button data-range="30d" class="active">30D</button>
      <button data-range="all">All</button>
    </div>
    <div id="perf-tiles" class="stat-tiles"></div>
    <div class="chart-card">
      <div class="chart-head"><h3>Cumulative R</h3><span class="cur" id="perf-chart-cur"></span></div>
      <svg class="chart-svg" id="perf-chart" viewBox="0 0 640 200" preserveAspectRatio="none"></svg>
      <p class="empty" id="perf-chart-empty" style="display:none">No resolved signals in this range yet.</p>
    </div>
    <div class="block-head"><h2>By Pair</h2></div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Pair</th><th>Signals</th><th>Win Rate</th><th>Avg R</th>
          <th>Directional Acc.</th><th>Best</th><th>Worst</th></tr></thead>
        <tbody id="perf-by-pair"></tbody>
      </table>
    </div>
    <div class="block-head"><h2>Recent Signals</h2><span class="hint">Newest first, up to 50 shown</span></div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Fired</th><th>Pair</th><th>Call</th><th>Entry</th>
          <th>Outcome</th><th>R</th><th>Directional</th></tr></thead>
        <tbody id="perf-signals"></tbody>
      </table>
    </div>
    <script id="perf-data" type="application/json">{data_json}</script>"""


def render_research_view(health_payload: dict, kpi_payload: dict) -> str:
    """Stage 2 prospective observation ("Research"). Like Performance, this
    renders a static shell at page load and does all real rendering
    client-side from embedded JSON -- health_payload/kpi_payload are
    exactly what research_snapshot_publisher.py last wrote (or an explicit
    not_published/malformed_snapshot_file placeholder), never computed
    here. The client then re-fetches /research/health (frequent) and
    /research/kpi (manual refresh button, less frequent) independently --
    see the script block in render_page() for the actual rendering logic,
    kept there so it shares helpers (fmtR etc.) with the Performance view."""
    data_json = json.dumps({
        "health": health_payload,
        "kpi": kpi_payload,
        "health_stale_after_seconds": RESEARCH_HEALTH_STALE_AFTER_SECONDS,
        "kpi_stale_after_seconds": RESEARCH_KPI_STALE_AFTER_SECONDS,
    })
    return f"""
    <div class="research-disclaimer">
      Paper observation only — a hypothetical decision scored against real, sampled bid/ask quotes.
      No order has ever been placed. Financing/swap charges, true fill slippage, and price movement
      between quote samples are <strong>not</strong> observed or estimated anywhere on this page.
    </div>

    <div class="block-head"><h2>Observation Health</h2><span class="hint" id="research-health-freshness"></span></div>
    <div id="research-health-container"><p class="empty">Loading…</p></div>

    <div class="block-head"><h2>Progress &amp; Performance</h2>
      <button class="refresh-btn" id="research-kpi-refresh">Refresh</button>
      <span class="hint" id="research-kpi-freshness"></span>
    </div>
    <div id="research-kpi-tiles-container"><p class="empty">Loading…</p></div>

    <div class="chart-card">
      <div class="chart-head"><h3>Cumulative Completed-Trade R</h3><span class="cur" id="research-chart-cur"></span></div>
      <div class="hint" id="research-chart-partial-note"></div>
      <svg class="chart-svg" id="research-chart" viewBox="0 0 640 220" preserveAspectRatio="none"></svg>
      <p class="empty" id="research-chart-empty" style="display:none">No completed trades yet.</p>
    </div>

    <div class="block-head"><h2>Trade Ledger</h2><span class="hint" id="research-ledger-scope">Click a row for timing &amp; coverage detail</span></div>
    <div class="table-wrap">
      <table>
        <thead><tr><th></th><th>Pair</th><th>Dir</th><th>Entry cond. / Stop / Target</th>
          <th>Paper Entry</th><th>Paper Exit</th><th>Outcome</th><th>R</th></tr></thead>
        <tbody id="research-ledger-body"></tbody>
      </table>
    </div>
    <div class="block-head"><h2>Suppressed Decisions</h2>
      <span class="hint">Same-pair decisions suppressed by the one-position-per-pair policy — not counted as executable opportunities</span></div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Pair</th><th>Dir</th><th>Published</th><th>Suppressed until</th></tr></thead>
        <tbody id="research-suppressed-body"></tbody>
      </table>
    </div>

    <div class="block-head"><h2>Roadmap</h2></div>
    <ul class="roadmap-list" id="research-roadmap"></ul>

    <script id="research-data" type="application/json">{data_json}</script>"""


def _v2_perf_class(v) -> str:
    """Same neutral-by-default discipline as the v1 Research view's
    perfClass(): null/undefined/non-finite never selects positive styling."""
    return "" if v is None else ("pos" if v >= 0 else "neg")


def render_research_v2_view(health_payload: dict, kpi_payload: dict) -> str:
    """v2 measurement contract -- server-rendered (no client JS needed for
    this compact activation-handoff view; deliberately simpler than v1's
    full chart+ledger UI, see research/prospective_baseline_v2's own
    README for why). Reads ONLY the pre-computed v2 snapshot files passed
    in -- same no-computation-in-the-handler discipline as v1. Rendered as
    its own section with its own heading and a loud never-combine banner;
    nothing here is merged with the v1 figures above."""
    def state_block(payload: dict) -> str | None:
        state = payload.get("state")
        if state == "ok":
            return None
        label = {
            "not_published": "No v2 snapshot published yet — the v2 publisher has not run.",
            "malformed_snapshot_file": "v2 snapshot file is malformed — waiting for the next publish.",
            "observer_not_found": "v2 observer checkout not found by the publisher.",
            "no_manifest": "v2 observer has not recorded a run manifest yet — no v2 activation detected.",
            "manifest_mismatch": "v2 publisher paused: code/contract changed since this run started.",
        }.get(state, state)
        if state == "error":
            label = payload.get("error") or "v2 snapshot generation failed."
        return f'<p class="empty">{html.escape(str(label))}</p>'

    health_block = state_block(health_payload)
    if health_block is None:
        ri = health_payload.get("run_identity") or {}
        svc = (health_payload.get("service") or {}).get("status", "unknown")
        rec = health_payload.get("recording_health", "unknown")
        health_block = f"""
        <div class="status-grid">
          <div>Service: <span class="status-pill status-{html.escape(svc)}">{html.escape(svc)}</span></div>
          <div>Recording: <span class="status-pill status-{html.escape(rec)}">{html.escape(rec.replace('_',' '))}</span></div>
          <div><span class="hint">v2 Run ID</span><br><span class="mono" title="{html.escape(ri.get('run_id',''))}">{html.escape((ri.get('run_id') or '—')[:12])}…</span></div>
        </div>"""

    kpi_block = state_block(kpi_payload)
    if kpi_block is None:
        kpi = kpi_payload.get("kpi") or {}
        c = kpi.get("counts", {})
        completed_avg = kpi.get("avg_net_r_per_completed_trade", {})
        kpi_block = f"""
        <div class="stat-tiles">
          <div class="tile"><div class="label">Eligible Alerts</div><div class="value">{c.get('eligible_alerts', '—')}</div>
            <div class="sub">{c.get('suppressed_existing_position', 0)} suppressed</div></div>
          <div class="tile"><div class="label">Entered / Completed</div><div class="value">{c.get('entered','—')} / {c.get('completed','—')}</div>
            <div class="sub">{c.get('pending_open',0)} pending · {c.get('unknown_total',0)} unknown</div></div>
          <div class="tile"><div class="label">Avg R / Completed</div>
            <div class="value {_v2_perf_class(completed_avg.get('value'))}">{fmt_r(completed_avg.get('value'))}</div>
            <div class="sub">n={completed_avg.get('denominator', 0)}</div></div>
          <div class="tile"><div class="label">Via Closure-Delayed Deadline</div><div class="value">{c.get('completed_via_closure_delayed_deadline', 0)}</div>
            <div class="sub">new v2 exit policy — see MEASUREMENT_CONTRACT.md §4</div></div>
        </div>"""

    return f"""
    <div class="block-head" style="margin-top:48px;border-top:2px solid var(--warn);padding-top:28px">
      <h2 style="color:var(--warn)">Research v2 — New Measurement Contract</h2>
    </div>
    <div class="research-disclaimer" style="border-left-color:var(--warn)">
      <strong>Separate observer, separate run identity, separate logs.</strong> v2 corrects a measurement-contract
      defect (price-creation age was wrongly used as a collection-health gate — see
      research/prospective_baseline_v2/MEASUREMENT_CONTRACT.md) found in the v1 7-day review. These figures are
      <strong>never</strong> combined with the v1 Research section above — different run, different contract,
      not directly comparable without reading the contract doc first.
    </div>
    <div class="block-head"><h3>v2 Observation Health</h3></div>
    <div>{health_block}</div>
    <div class="block-head"><h3>v2 Progress &amp; Performance</h3></div>
    <div>{kpi_block}</div>
    """


def render_page(live_payload: dict | None, performance_payload: dict | None, alerts_payload: dict | None,
                 research_health_payload: dict, research_kpi_payload: dict,
                 research_health_v2_payload: dict, research_kpi_v2_payload: dict) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<title>Signal IQ</title>
<style>
  :root {{
    --ground:#090d14; --surface:#10161f; --surface-2:#161d29; --surface-3:#1c2432;
    --border:rgba(158,176,204,0.12); --border-strong:rgba(158,176,204,0.22);
    --text:#e9edf5; --text-muted:#8996ac; --text-faint:#5c6577;
    --accent:#4c7eff; --accent-hover:#6a95ff; --accent-soft:rgba(76,126,255,0.14);
    --long:#34c495; --long-soft:rgba(52,196,149,0.14);
    --short:#f1654a; --short-soft:rgba(241,101,74,0.14);
    --neutral:#8996ac; --neutral-soft:rgba(137,150,172,0.12);
    --warn:#e0a94c; --warn-soft:rgba(224,169,76,0.14);
    --font-display:'Sora',system-ui,sans-serif; --font-body:'Inter',system-ui,sans-serif;
    --font-mono:'IBM Plex Mono',ui-monospace,'SF Mono',Menlo,monospace;
    --radius:12px; --radius-sm:8px;
  }}
  * {{ box-sizing:border-box; }}
  html {{ color-scheme: dark; }}
  body {{ margin:0; background:var(--ground); color:var(--text); font-family:var(--font-body); line-height:1.5; }}
  h1,h2,h3 {{ font-family:var(--font-display); margin:0; }}
  .mono {{ font-family:var(--font-mono); font-variant-numeric:tabular-nums; }}

  .shell {{ display:grid; grid-template-columns:220px 1fr; min-height:100vh; }}
  .sidebar {{ border-right:1px solid var(--border); padding:24px 16px; display:flex; flex-direction:column; gap:24px; position:sticky; top:0; height:100vh; }}
  .brand {{ display:flex; align-items:center; gap:10px; padding:0 8px; }}
  .brand-mark {{ width:28px; height:28px; border-radius:8px; background:linear-gradient(155deg,var(--accent),#2a4fc4); flex-shrink:0; }}
  .brand-name {{ font-family:var(--font-display); font-weight:700; font-size:16px; }}
  .nav {{ display:flex; flex-direction:column; gap:2px; }}
  .nav-item {{ display:flex; align-items:center; gap:10px; padding:9px 12px; border-radius:var(--radius-sm); background:transparent; border:none; cursor:pointer; text-align:left; color:var(--text-muted); font-family:var(--font-display); font-weight:600; font-size:13.5px; }}
  .nav-item:hover {{ background:var(--surface-2); color:var(--text); }}
  .nav-item.active {{ background:var(--accent-soft); color:var(--accent-hover); }}

  .main {{ min-width:0; }}
  .topbar {{ padding:20px clamp(16px,3vw,36px); border-bottom:1px solid var(--border); }}
  .topbar h1 {{ font-size:19px; font-weight:700; }}
  .view {{ display:none; padding:24px clamp(16px,3vw,36px) 80px; max-width:1180px; }}
  .view.active {{ display:block; }}
  .block-head {{ display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin:28px 0 14px; flex-wrap:wrap; }}
  .block-head h2 {{ font-size:15px; font-weight:700; }}
  .hint {{ font-size:12.5px; color:var(--text-faint); }}
  .badge.outcome-target {{ background:var(--long-soft); color:var(--long); }}
  .badge.outcome-stop {{ background:var(--short-soft); color:var(--short); }}
  .badge.outcome-unresolved, .badge.outcome-no_data {{ background:var(--neutral-soft); color:var(--neutral); }}
  .badge.dir-correct {{ background:var(--long-soft); color:var(--long); }}
  .badge.dir-incorrect {{ background:var(--short-soft); color:var(--short); }}
  .badge.dir-pending, .badge.dir-no_data {{ background:var(--neutral-soft); color:var(--neutral); }}

  .subtitle {{ color:var(--text-faint); font-size:13px; margin-bottom:20px; font-variant-numeric:tabular-nums; }}
  .empty {{ color:var(--text-muted); }}
  .table-wrap {{ overflow-x:auto; border:1px solid var(--border); border-radius:var(--radius); }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; min-width:640px; }}
  th, td {{ padding:10px 14px; text-align:left; border-bottom:1px solid var(--border); vertical-align:top; }}
  thead th {{ background:var(--surface-2); font-size:10.5px; text-transform:uppercase; letter-spacing:0.06em; color:var(--text-faint); font-weight:600; white-space:nowrap; }}
  tbody td {{ background:var(--surface); }}
  tbody tr:last-child td {{ border-bottom:none; }}
  .pair {{ font-weight:700; font-family:var(--font-display); }}
  .num {{ font-variant-numeric:tabular-nums; font-family:var(--font-mono); }}
  .arrow {{ font-size:11px; font-family:var(--font-body); }}
  .arrow-up {{ color:var(--long); }}
  .arrow-down {{ color:var(--short); }}
  .arrow-flat {{ color:var(--text-faint); }}
  .sparkline {{ width:72px; height:24px; display:block; }}
  .sparkline-empty {{ color:var(--text-faint); }}
  .tech-breakdown {{ color:var(--text-muted); font-size:12px; font-family:var(--font-mono); }}
  .composite {{ color:var(--text); font-weight:600; margin-top:2px; }}
  .combined {{ font-weight:700; font-size:14px; }}
  .badge {{ display:inline-block; padding:2px 9px; border-radius:999px; font-size:11px; font-weight:700; letter-spacing:0.03em; font-family:var(--font-body); }}
  .badge.long {{ background:var(--long-soft); color:var(--long); }}
  .badge.short {{ background:var(--short-soft); color:var(--short); }}
  .badge.neutral {{ background:var(--neutral-soft); color:var(--neutral); }}
  .badge.conf-high {{ background:var(--accent-soft); color:var(--accent-hover); }}
  .badge.conf-medium {{ background:var(--warn-soft); color:var(--warn); }}
  .badge.conf-low {{ background:var(--neutral-soft); color:var(--text-faint); }}
  .conf {{ color:var(--text-faint); font-size:11px; text-transform:uppercase; }}
  .sltp {{ font-size:12px; line-height:1.6; }}
  .reason-row td {{ border-bottom:1px solid var(--border); padding-top:0; }}
  .reason {{ color:var(--text-faint); font-size:12px; }}
  .window {{ color:var(--accent-hover); font-size:11px; margin-top:2px; }}
  .err-row .err {{ color:var(--short); font-size:12px; }}
  tr[data-pair].flash td {{ animation: flash-row 1s ease-in-out 3; }}
  @keyframes flash-row {{ 0%,100% {{ background-color:transparent; }} 50% {{ background-color:rgba(76,126,255,0.22); }} }}

  .feed {{ display:flex; flex-direction:column; gap:12px; margin-bottom:8px; }}
  .card {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:18px 20px; position:relative; overflow:hidden; }}
  .card::before {{ content:""; position:absolute; left:0; top:0; bottom:0; width:3px; }}
  .card.dir-long::before {{ background:var(--long); }}
  .card.dir-short::before {{ background:var(--short); }}
  .card.dir-flat::before {{ background:var(--neutral); }}
  .card-top {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-bottom:14px; }}
  .card-id {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
  .pair-name {{ font-family:var(--font-display); font-weight:700; font-size:16px; }}
  .card-meta {{ text-align:right; font-size:12px; color:var(--text-faint); }}
  .card-meta .entry {{ font-size:14px; color:var(--text); font-weight:500; }}
  .card-grid {{ display:grid; grid-template-columns:1.1fr 1fr 1fr; gap:20px; }}
  .card-section-label {{ font-size:10.5px; text-transform:uppercase; letter-spacing:0.07em; color:var(--text-faint); font-weight:600; margin-bottom:9px; }}
  .bar-row {{ display:flex; align-items:center; gap:8px; margin-bottom:7px; font-size:12px; }}
  .bar-row:last-child {{ margin-bottom:0; }}
  .bar-label {{ width:56px; flex-shrink:0; color:var(--text-muted); }}
  .bar-track {{ flex:1; height:5px; border-radius:999px; background:var(--surface-3); position:relative; overflow:hidden; }}
  .bar-fill {{ position:absolute; top:0; bottom:0; border-radius:999px; }}
  .bar-fill.pos {{ background:var(--long); left:50%; }}
  .bar-fill.neg {{ background:var(--short); right:50%; }}
  .bar-mid {{ position:absolute; left:50%; top:-1px; bottom:-1px; width:1px; background:var(--border-strong); }}
  .bar-val {{ width:38px; text-align:right; font-size:11.5px; color:var(--text-muted); flex-shrink:0; }}
  .evidence {{ display:flex; flex-direction:column; gap:5px; }}
  .evidence-item {{ font-size:11.5px; line-height:1.4; color:var(--text-muted); padding-left:10px; position:relative; }}
  .evidence-item::before {{ content:""; position:absolute; left:0; top:6px; width:4px; height:4px; border-radius:50%; background:var(--text-faint); }}
  .evidence-item .src {{ color:var(--text-faint); }}
  .card-footer {{ display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; margin-top:16px; padding-top:14px; border-top:1px solid var(--border); font-size:12px; }}
  .sltp-group {{ display:flex; gap:18px; }}
  .sltp-group .k {{ color:var(--text-faint); margin-right:5px; }}
  .sltp-group .v {{ color:var(--text); }}
  .window-note {{ color:var(--text-faint); }}
  .window-note .session {{ color:var(--accent-hover); font-weight:600; }}
  .standdown-note {{ font-size:12.5px; color:var(--text-muted); margin:0; }}
  article[data-alert-id].flash {{ animation: flash-row 1s ease-in-out 3; }}

  .range-toggle {{ display:inline-flex; background:var(--surface-2); border:1px solid var(--border); border-radius:999px; padding:3px; gap:2px; margin-bottom:20px; }}
  .range-toggle button {{ border:none; background:transparent; padding:6px 14px; border-radius:999px; font-size:12.5px; font-weight:600; color:var(--text-faint); cursor:pointer; font-family:var(--font-body); }}
  .range-toggle button.active {{ background:var(--accent); color:#fff; }}
  .stat-tiles {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:24px; }}
  .tile {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:16px 18px; }}
  .tile .label {{ font-size:11px; text-transform:uppercase; letter-spacing:0.06em; color:var(--text-faint); font-weight:600; margin-bottom:8px; }}
  .tile .value {{ font-family:var(--font-mono); font-size:22px; font-weight:500; }}
  .tile .value.pos {{ color:var(--long); }}
  .tile .value.neg {{ color:var(--short); }}
  .tile .sub {{ font-size:11.5px; color:var(--text-faint); margin-top:4px; }}
  .chart-card {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:20px 22px 10px; margin-bottom:8px; }}
  .chart-head {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:4px; }}
  .chart-head h3 {{ font-size:13.5px; font-weight:700; }}
  .chart-head .cur {{ font-family:var(--font-mono); color:var(--long); font-weight:500; }}
  .chart-svg {{ width:100%; height:200px; display:block; }}

  /* ---------- Research ---------- */
  .research-disclaimer {{ background:var(--surface); border:1px solid var(--border); border-left:3px solid var(--warn); border-radius:var(--radius-sm); padding:12px 16px; font-size:12.5px; color:var(--text-muted); margin-bottom:24px; line-height:1.55; }}
  .refresh-btn {{ background:var(--surface-2); border:1px solid var(--border); color:var(--text-muted); font-family:var(--font-body); font-weight:600; font-size:12px; padding:5px 12px; border-radius:999px; cursor:pointer; }}
  .refresh-btn:hover {{ background:var(--surface-3); color:var(--text); }}
  .refresh-btn:disabled {{ opacity:0.5; cursor:default; }}
  .status-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px 16px; margin-bottom:16px; }}
  .status-grid .mono {{ overflow-wrap:anywhere; }}
  .status-pill {{ display:inline-flex; align-items:center; gap:6px; padding:3px 10px; border-radius:999px; font-size:11.5px; font-weight:700; }}
  .status-pill.status-ok, .status-pill.status-active, .status-pill.status-healthy {{ background:var(--long-soft); color:var(--long); }}
  .status-pill.status-warn, .status-pill.status-partial, .status-pill.status-stale {{ background:var(--warn-soft); color:var(--warn); }}
  .status-pill.status-bad, .status-pill.status-inactive, .status-pill.status-failed, .status-pill.status-no_data {{ background:var(--short-soft); color:var(--short); }}
  .status-pill.status-unknown {{ background:var(--neutral-soft); color:var(--neutral); }}
  .pair-coverage-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(110px,1fr)); gap:8px; margin-top:12px; }}
  .pair-cov-cell {{ background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm); padding:8px 10px; font-size:11.5px; }}
  .pair-cov-cell .pc-pair {{ font-weight:700; font-family:var(--font-display); margin-bottom:3px; }}
  .pair-cov-cell .pc-age {{ color:var(--text-faint); font-family:var(--font-mono); }}
  .pair-cov-cell.pc-fresh {{ border-color:rgba(52,196,149,0.35); }}
  .pair-cov-cell.pc-stale {{ border-color:rgba(241,101,74,0.35); }}
  .snapshot-note {{ font-size:12px; color:var(--text-faint); margin:10px 0 18px; }}
  .snapshot-note.is-stale {{ color:var(--warn); }}
  .undetermined {{ color:var(--text-faint); font-style:italic; }}
  .roadmap-list {{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:10px; }}
  .roadmap-list li {{ display:flex; align-items:flex-start; gap:12px; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:14px 16px; }}
  .roadmap-list .rm-dot {{ width:10px; height:10px; border-radius:50%; margin-top:4px; flex-shrink:0; }}
  .roadmap-list .rm-dot.complete {{ background:var(--long); }}
  .roadmap-list .rm-dot.active {{ background:var(--accent); }}
  .roadmap-list .rm-dot.pending {{ background:var(--text-faint); }}
  .roadmap-list .rm-title {{ font-weight:700; font-family:var(--font-display); font-size:13.5px; margin-bottom:3px; }}
  .roadmap-list .rm-detail {{ font-size:12px; color:var(--text-muted); line-height:1.5; }}
  .ledger-row {{ cursor:pointer; }}
  .ledger-row:hover td {{ background:var(--surface-2); }}
  .ledger-row .chevron {{ display:inline-block; transition:transform 0.15s; color:var(--text-faint); }}
  .ledger-row.expanded .chevron {{ transform:rotate(90deg); }}
  .ledger-detail-row td {{ background:var(--surface-2); padding:14px 16px; font-size:12px; color:var(--text-muted); }}
  .ledger-detail-row dl {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(180px,1fr)); gap:8px 20px; margin:0; }}
  .ledger-detail-row dt {{ color:var(--text-faint); font-size:10.5px; text-transform:uppercase; letter-spacing:0.05em; }}
  .ledger-detail-row dd {{ margin:2px 0 0; font-family:var(--font-mono); color:var(--text); }}
  .reason-text {{ color:var(--warn); }}

  .bottom-nav {{ display:none; }}
  @media (max-width:860px) {{
    .shell {{ grid-template-columns:1fr; }}
    .sidebar {{ display:none; }}
    .main {{ padding-bottom:64px; }}
    .bottom-nav {{ display:flex; position:fixed; bottom:0; left:0; right:0; z-index:20; background:color-mix(in srgb, var(--surface) 92%, transparent); backdrop-filter:blur(12px); border-top:1px solid var(--border); padding:8px 10px calc(8px + env(safe-area-inset-bottom)); justify-content:space-around; }}
    .bottom-nav button {{ background:none; border:none; display:flex; flex-direction:column; align-items:center; gap:3px; color:var(--text-faint); font-size:10.5px; font-weight:600; font-family:var(--font-display); padding:4px 18px; border-radius:var(--radius-sm); cursor:pointer; }}
    .bottom-nav button.active {{ color:var(--accent-hover); }}
    .stat-tiles {{ grid-template-columns:repeat(2,1fr); }}
    .card-grid {{ grid-template-columns:1fr; gap:16px; }}
  }}
</style></head><body>
<div class="shell">
  <aside class="sidebar">
    <div class="brand"><div class="brand-mark"></div><div class="brand-name">Signal IQ</div></div>
    <nav class="nav" id="side-nav">
      <button class="nav-item active" data-view="live">Live</button>
      <button class="nav-item" data-view="performance">Performance</button>
      <button class="nav-item" data-view="research">Research</button>
    </nav>
  </aside>
  <main class="main">
    <section class="view active" id="view-live">
      <div class="topbar"><h1>Live</h1></div>
      <div style="padding:24px clamp(16px,3vw,36px) 0">
        <div class="block-head"><h2>Live Feed</h2></div>
        <div id="feed-container">{render_feed_body(alerts_payload)}</div>
        <div class="block-head"><h2>Market</h2></div>
        <div id="table-container">{render_table_body(live_payload)}</div>
      </div>
    </section>
    <section class="view" id="view-performance">
      <div class="topbar"><h1>Performance</h1></div>
      <div style="padding:24px clamp(16px,3vw,36px) 0" id="performance-container">
        {render_performance_view(performance_payload)}
      </div>
    </section>
    <section class="view" id="view-research">
      <div class="topbar"><h1>Research</h1></div>
      <div style="padding:24px clamp(16px,3vw,36px) 0" id="research-container">
        {render_research_view(research_health_payload, research_kpi_payload)}
        {render_research_v2_view(research_health_v2_payload, research_kpi_v2_payload)}
      </div>
    </section>
  </main>
  <nav class="bottom-nav" id="bottom-nav">
    <button class="active" data-view="live">Live</button>
    <button data-view="performance">Performance</button>
    <button data-view="research">Research</button>
  </nav>
</div>
<script>
  const navButtons = document.querySelectorAll('.nav-item, .bottom-nav button');
  function setView(name) {{
    document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
    navButtons.forEach(b => b.classList.toggle('active', b.dataset.view === name));
  }}
  navButtons.forEach(b => b.addEventListener('click', () => setView(b.dataset.view)));

  // ---------- Live: table refresh + alert polling/notifications ----------
  let lastAlertId = null;

  function beep() {{
    try {{
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain); gain.connect(ctx.destination);
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.15, ctx.currentTime);
      osc.start();
      osc.stop(ctx.currentTime + 0.18);
    }} catch (e) {{ /* audio not available */ }}
  }}

  function flashRow(pair) {{
    document.querySelectorAll(`tr[data-pair="${{pair}}"]`).forEach(row => {{
      row.classList.add('flash');
      setTimeout(() => row.classList.remove('flash'), 3000);
    }});
  }}

  async function refreshTable() {{
    try {{
      const resp = await fetch('/table', {{ credentials: 'same-origin' }});
      if (!resp.ok) return;
      document.getElementById('table-container').innerHTML = await resp.text();
    }} catch (e) {{ /* transient — next poll retries */ }}
  }}

  async function refreshFeed() {{
    try {{
      const resp = await fetch('/feed', {{ credentials: 'same-origin' }});
      if (!resp.ok) return;
      document.getElementById('feed-container').innerHTML = await resp.text();
    }} catch (e) {{ /* transient — next poll retries */ }}
  }}

  async function pollAlerts() {{
    try {{
      const resp = await fetch('/alerts', {{ credentials: 'same-origin' }});
      if (!resp.ok) return;
      const data = await resp.json();
      const alerts = data.alerts || [];
      if (lastAlertId === null) {{
        lastAlertId = alerts.length ? alerts[alerts.length - 1].id : '';
        return;
      }}
      const idx = alerts.findIndex(a => a.id === lastAlertId);
      const fresh = idx === -1 ? alerts : alerts.slice(idx + 1);
      if (fresh.length) refreshFeed();  // pulls in the new card(s) via the same render_feed_body() the page loaded with
      for (const a of fresh) {{
        if ('Notification' in window && Notification.permission === 'granted') {{
          new Notification(`${{a.pair}} — ${{a.direction.toUpperCase()}}`, {{ body: a.message }});
        }}
        beep();
        flashRow(a.pair);
      }}
      if (alerts.length) lastAlertId = alerts[alerts.length - 1].id;
    }} catch (e) {{ /* transient — next poll retries */ }}
  }}

  if ('Notification' in window && Notification.permission === 'default') {{
    Notification.requestPermission();
  }}
  setInterval(refreshTable, 15000);
  setInterval(pollAlerts, 5000);
  pollAlerts();

  // ---------- Performance: render from embedded data, range-toggle client-side ----------
  const perfDataEl = document.getElementById('perf-data');
  if (perfDataEl) {{
    const perfData = JSON.parse(perfDataEl.textContent);
    const aggregates = perfData.aggregates;
    const allSignals = perfData.signals;
    const RANGE_DAYS = {{ '7d': 7, '30d': 30, 'all': null }};

    function fmtPct(v) {{ return (v === null || v === undefined) ? '—' : (v * 100).toFixed(0) + '%'; }}
    function fmtR(v) {{ return (v === null || v === undefined) ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2) + 'R'; }}
    function fmtTime(iso) {{ return iso.replace('T', ' ').slice(0, 16) + ' UTC'; }}
    const OUTCOME_LABEL = {{ target: 'Target hit', stop: 'Stopped out', unresolved: 'Unresolved', no_data: 'No data' }};
    const DIRECTIONAL_LABEL = {{ correct: 'Correct', incorrect: 'Incorrect', pending: 'Pending', no_data: 'No data' }};

    function renderPerformanceRange(range) {{
      const agg = aggregates[range];
      const pairCount = Object.keys(agg.by_pair).length;
      document.getElementById('perf-tiles').innerHTML = `
        <div class="tile"><div class="label">Win Rate</div><div class="value ${{agg.win_rate >= 0.5 ? 'pos' : ''}}">${{fmtPct(agg.win_rate)}}</div><div class="sub">${{agg.resolved_signals}} of ${{agg.total_signals}} signals</div></div>
        <div class="tile"><div class="label">Avg R</div><div class="value ${{agg.avg_r >= 0 ? 'pos' : 'neg'}}">${{fmtR(agg.avg_r)}}</div><div class="sub">per closed signal</div></div>
        <div class="tile"><div class="label">Directional Accuracy</div><div class="value ${{agg.directional_accuracy >= 0.5 ? 'pos' : ''}}">${{fmtPct(agg.directional_accuracy)}}</div><div class="sub">${{agg.directional_sample_size}} scored</div></div>
        <div class="tile"><div class="label">Total Signals</div><div class="value">${{agg.total_signals}}</div><div class="sub">${{pairCount}} pair${{pairCount === 1 ? '' : 's'}}</div></div>`;

      const series = agg.cumulative_r_series;
      const chartEmpty = document.getElementById('perf-chart-empty');
      const chartSvg = document.getElementById('perf-chart');
      if (!series.length) {{
        chartSvg.style.display = 'none';
        chartEmpty.style.display = 'block';
        document.getElementById('perf-chart-cur').textContent = '';
      }} else {{
        chartSvg.style.display = 'block';
        chartEmpty.style.display = 'none';
        const W = 640, H = 200, pad = 8;
        const values = series.map(p => p.cumulative_r);
        const max = Math.max(0, ...values), min = Math.min(0, ...values);
        const range_ = (max - min) || 1;
        const xs = i => pad + (i / Math.max(series.length - 1, 1)) * (W - pad * 2);
        const ys = v => H - pad - ((v - min) / range_) * (H - pad * 2);
        const points = series.map((p, i) => `${{xs(i)}},${{ys(p.cumulative_r)}}`).join(' ');
        const areaPoints = points + ` ${{xs(series.length - 1)}},${{H - pad}} ${{xs(0)}},${{H - pad}}`;
        const last = series[series.length - 1];
        const lastX = xs(series.length - 1), lastY = ys(last.cumulative_r);
        const lineColor = last.cumulative_r >= 0 ? 'var(--long)' : 'var(--short)';
        chartSvg.innerHTML = `
          <defs><linearGradient id="perfAreaFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="${{lineColor}}" stop-opacity="0.28" />
            <stop offset="100%" stop-color="${{lineColor}}" stop-opacity="0" />
          </linearGradient></defs>
          <line x1="${{pad}}" y1="${{ys(0)}}" x2="${{W-pad}}" y2="${{ys(0)}}" stroke="var(--border)" stroke-width="1" />
          <polygon points="${{areaPoints}}" fill="url(#perfAreaFill)" />
          <polyline points="${{points}}" fill="none" stroke="${{lineColor}}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
          <circle cx="${{lastX}}" cy="${{lastY}}" r="4" fill="${{lineColor}}" />`;
        document.getElementById('perf-chart-cur').textContent = fmtR(last.cumulative_r);
      }}

      document.getElementById('perf-by-pair').innerHTML = Object.entries(agg.by_pair).map(([pair, s]) => `
        <tr><td class="pair">${{pair}}</td><td class="num">${{s.signals}}</td>
          <td class="num">${{fmtPct(s.win_rate)}}</td><td class="num">${{fmtR(s.avg_r)}}</td>
          <td class="num">${{fmtPct(s.directional_accuracy)}}</td>
          <td class="num">${{fmtR(s.best_r)}}</td><td class="num">${{fmtR(s.worst_r)}}</td></tr>`).join('');

      const days = RANGE_DAYS[range];
      const cutoff = days === null ? null : Date.now() - days * 86400000;
      const inRange = allSignals.filter(s => cutoff === null || new Date(s.logged_at).getTime() >= cutoff);
      const sorted = inRange.slice().sort((a, b) => new Date(b.logged_at) - new Date(a.logged_at)).slice(0, 50);
      document.getElementById('perf-signals').innerHTML = sorted.length
        ? sorted.map(s => `
          <tr>
            <td class="mono">${{fmtTime(s.logged_at)}}</td>
            <td class="pair">${{s.pair}}</td>
            <td><span class="badge ${{s.direction}}">${{s.direction.toUpperCase()}}</span> <span class="conf">${{s.confidence}}</span></td>
            <td class="num">${{fmt5(s.entry)}}</td>
            <td><span class="badge outcome-${{s.outcome}}">${{OUTCOME_LABEL[s.outcome] || s.outcome}}</span></td>
            <td class="num">${{fmtR(s.r_multiple)}}</td>
            <td><span class="badge dir-${{s.directional_outcome}}">${{DIRECTIONAL_LABEL[s.directional_outcome] || s.directional_outcome}}</span></td>
          </tr>`).join('')
        : '<tr><td colspan="7" class="empty">No signals in this range.</td></tr>';
    }}

    function fmt5(v) {{ return (v === null || v === undefined) ? '—' : v.toFixed(5); }}

    document.getElementById('perf-range-toggle').addEventListener('click', (e) => {{
      const btn = e.target.closest('button');
      if (!btn) return;
      document.querySelectorAll('#perf-range-toggle button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      renderPerformanceRange(btn.dataset.range);
    }});

    renderPerformanceRange('30d');
  }}

  // ---------- Research: health (frequent poll) + KPI/ledger (manual/slow refresh) ----------
  const researchDataEl = document.getElementById('research-data');
  if (researchDataEl) {{
    const initialResearch = JSON.parse(researchDataEl.textContent);
    const healthStaleAfter = initialResearch.health_stale_after_seconds;
    const kpiStaleAfter = initialResearch.kpi_stale_after_seconds;
    let latestHealth = initialResearch.health;
    let latestKpiPayload = initialResearch.kpi;

    function healthAgeSeconds(health) {{
      const genAt = health && health.generated_at_utc ? new Date(health.generated_at_utc) : null;
      return genAt ? (Date.now() - genAt.getTime()) / 1000 : null;
    }}

    // Compares health.run_identity.run_id against kpi.run_id -- only when
    // BOTH snapshots are in state "ok" and both actually carry a run_id;
    // otherwise there's nothing to compare yet (not a mismatch, just
    // unknown -- the individual explicit-state panels already cover that
    // case). Recomputed on demand by renderKpiView(), which every refresh
    // path (health OR kpi) calls, so a mismatch that appears or resolves
    // on either side is caught immediately, not just on the next kpi poll.
    function runIdMismatch() {{
      const h = latestHealth, k = latestKpiPayload;
      const healthRunId = (h && h.state === 'ok' && h.run_identity) ? h.run_identity.run_id : null;
      const kpiRunId = (k && k.state === 'ok') ? k.run_id : null;
      if (!healthRunId || !kpiRunId) return null;
      return healthRunId !== kpiRunId ? {{ healthRunId, kpiRunId }} : null;
    }}

    function fmtUTC2(iso) {{ return iso ? iso.replace('T', ' ').slice(0, 19) + ' UTC' : '—'; }}
    // Neutral-by-default number handling: null, undefined, NaN and
    // +/-Infinity are all "not a usable number" and must never be
    // coerced into a pos/neg comparison (e.g. `undefined >= 0` is false
    // in JS, which would otherwise silently paint a missing value red).
    function isFiniteNum(v) {{ return typeof v === 'number' && Number.isFinite(v); }}
    function fmtR2(v) {{ return isFiniteNum(v) ? (v >= 0 ? '+' : '') + v.toFixed(2) + 'R' : '—'; }}
    function perfClass(v) {{ return isFiniteNum(v) ? (v >= 0 ? 'pos' : 'neg') : ''; }}
    function fmtAge(seconds) {{
      if (seconds === null || seconds === undefined) return '—';
      if (seconds < 90) return seconds.toFixed(0) + 's ago';
      if (seconds < 5400) return (seconds / 60).toFixed(0) + 'm ago';
      if (seconds < 172800) return (seconds / 3600).toFixed(1) + 'h ago';
      return (seconds / 86400).toFixed(1) + 'd ago';
    }}
    function fmtDays(d) {{ return (d === null || d === undefined) ? '—' : d.toFixed(1) + ' day' + (d.toFixed(1) === '1.0' ? '' : 's'); }}
    function statusPill(label, kind) {{ return `<span class="status-pill status-${{kind}}">${{label}}</span>`; }}
    // Snapshot-sourced text (error messages especially) is inserted into
    // innerHTML in several places below -- it must be escaped first.
    // Without this, a literal "<path>" in a redacted error message (see
    // research_snapshot_publisher.py's _short_error) is parsed as an
    // (unknown, invisible) HTML tag instead of displayed as text, and
    // disappears silently -- a real bug this caught during testing.
    function escapeHtml(s) {{
      return String(s).replace(/[&<>"']/g, ch => ({{'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}})[ch]);
    }}

    const SNAPSHOT_STATE_LABEL = {{
      not_published: 'No snapshot published yet — the publisher has not run.',
      malformed_snapshot_file: 'Snapshot file is malformed — waiting for the next publish.',
      observer_not_found: 'Observer checkout not found by the publisher.',
      no_manifest: 'Observer has not recorded a run manifest yet — no activation detected.',
      manifest_mismatch: 'Paused: the observer’s code/contract changed since this run started. A new run is required.',
    }};

    function explicitStateBlock(payload) {{
      if (payload.state === 'ok') return null;
      const label = payload.state === 'error' ? (payload.error || 'Snapshot generation failed.')
        : (SNAPSHOT_STATE_LABEL[payload.state] || `Unavailable (${{payload.state}}).`);
      return `<p class="empty">${{escapeHtml(label)}}</p>`;
    }}

    function renderHealth(health) {{
      latestHealth = health;
      const container = document.getElementById('research-health-container');
      const freshnessEl = document.getElementById('research-health-freshness');
      const explicit = explicitStateBlock(health);
      if (explicit) {{ container.innerHTML = explicit; freshnessEl.textContent = ''; renderRoadmap(health); renderKpiView(); return; }}

      const genAt = health.generated_at_utc ? new Date(health.generated_at_utc) : null;
      const ageSec = genAt ? (Date.now() - genAt.getTime()) / 1000 : null;
      const isStale = ageSec !== null && ageSec > healthStaleAfter;
      freshnessEl.innerHTML = genAt
        ? `<span class="snapshot-note ${{isStale ? 'is-stale' : ''}}">snapshot ${{fmtAge(ageSec)}}${{isStale ? ' — STALE' : ''}}</span>` : '';

      const svcKind = {{active: 'active', inactive: 'inactive', failed: 'failed'}}[health.service.status] || 'unknown';
      const recKind = {{healthy: 'healthy', partial: 'partial', stale: 'stale', no_data: 'no_data'}}[health.recording_health] || 'unknown';
      const ri = health.run_identity;

      const pairsHtml = Object.entries(health.quotes.pairs).map(([pair, p]) => {{
        const fresh = p.latest_receipt_age_seconds !== null && p.latest_receipt_age_seconds <= 180;
        return `<div class="pair-cov-cell ${{fresh ? 'pc-fresh' : 'pc-stale'}}">
          <div class="pc-pair">${{pair}}</div>
          <div class="pc-age">${{fmtAge(p.latest_receipt_age_seconds)}}</div>
          <div class="pc-age">${{p.valid_samples_in_window}}/${{p.samples_in_window}} valid${{p.has_gap ? ' · gap' : ''}}</div>
        </div>`;
      }}).join('');

      container.innerHTML = `
        <div class="status-grid">
          <div>Service: ${{statusPill(health.service.status, svcKind)}} <span class="hint">(${{health.service.name}})</span></div>
          <div>Recording: ${{statusPill(health.recording_health.replace('_',' '), recKind)}} <span class="hint">(inferred from quote arrivals — independent of service status)</span></div>
        </div>
        <div class="status-grid">
          <div><span class="hint">Run ID</span><br><span class="mono" title="${{ri ? ri.run_id : ''}}">${{ri ? ri.run_id.slice(0, 12) + '…' : '—'}}</span></div>
          <div><span class="hint">Activated</span><br><span class="mono">${{ri ? fmtUTC2(ri.started_at_utc) : '—'}}</span></div>
          <div><span class="hint">Elapsed</span><br><span class="mono">${{ri ? fmtDays(ri.elapsed_days) : '—'}}</span></div>
          <div><span class="hint">Latest quote receipt</span><br><span class="mono">${{fmtAge(health.quotes.latest_receipt_age_seconds)}}</span></div>
        </div>
        <div class="hint">Pair coverage: ${{health.quotes.pair_coverage_count}} / ${{health.quotes.pair_coverage_total}} fresh (last ${{Math.round(health.quotes.window_seconds/60)}}m window) —
          ${{health.quotes.invalid_or_stale_quotes_in_window}} invalid/stale of ${{health.quotes.total_quotes_in_window}} samples in window</div>
        <div class="pair-coverage-grid">${{pairsHtml}}</div>
        <div class="hint" style="margin-top:14px">Recording failures (this run): poll_failed=${{health.recording_failures.poll_failed}}
          quote_poll_failed=${{health.recording_failures.quote_poll_failed}} crashed=${{health.recording_failures.crashed}}</div>`;
      renderRoadmap(health);
      renderKpiView();  // a health refresh can newly create or resolve a run-id mismatch against the last-known KPI snapshot -- recheck every time, not just on a KPI refresh
    }}

    function renderRoadmap(health) {{
      const el = document.getElementById('research-roadmap');
      // run_identity can be present even off the "ok" path (the publisher
      // still attaches a sanitized run_identity on its generic "error"
      // state, since the manifest was already read before the failure) --
      // the review-checkpoint DATES are a fact about when the run started,
      // independent of whether we can currently confirm it's healthy, so
      // they're computed whenever a run_identity is available at all.
      const ri = health.run_identity || null;
      let review7 = '—', review30 = '—';
      if (ri && ri.started_at_utc) {{
        const started = new Date(ri.started_at_utc);
        review7 = fmtUTC2(new Date(started.getTime() + 7 * 86400000).toISOString());
        review30 = fmtUTC2(new Date(started.getTime() + 30 * 86400000).toISOString());
      }}

      // The "currently running" claim, by contrast, must be backed by
      // FRESH health evidence -- never asserted from a missing, stale, or
      // errored snapshot, and never collapsing service status into
      // recording health (a service can be "active" while genuinely not
      // recording, e.g. over a weekend, or vice versa mid-restart).
      const ageSec = healthAgeSeconds(health);
      const stale = health.state === 'ok' && ageSec !== null && ageSec > healthStaleAfter;
      let obsDot = 'pending', obsTitle = 'Prospective observation — status unknown', obsDetail;
      if (health.state !== 'ok') {{
        obsDetail = `Health snapshot unavailable (${{escapeHtml(health.state)}}${{health.error ? ': ' + escapeHtml(health.error) : ''}}) — cannot confirm whether observation is currently running.`;
      }} else if (stale) {{
        obsTitle = 'Prospective observation — status stale';
        obsDetail = `Health snapshot last updated ${{fmtAge(ageSec)}}, too old to confirm current activity. Last reported: service ${{health.service.status}}, recording ${{health.recording_health.replace('_', ' ')}}.`;
      }} else {{
        const svc = health.service.status, rec = health.recording_health;
        if (svc === 'active' && rec === 'healthy') {{
          obsDot = 'active';
          obsTitle = 'Prospective observation — active';
          obsDetail = 'Paper-only, real-time, currently running (service active, recording healthy — see Observation Health above).';
        }} else {{
          obsDot = svc === 'active' ? 'active' : 'pending';
          obsTitle = `Prospective observation — service ${{svc}}, recording ${{rec.replace('_', ' ')}}`;
          obsDetail = 'Service status and recording health are independent signals (see Observation Health above) — not necessarily fully operational.';
        }}
      }}
      obsDetail += ` 7-day data-quality checkpoint: <span class="mono">${{review7}}</span>. 30-day (or 50 completed outcomes, if sooner) performance checkpoint: <span class="mono">${{review30}}</span>. These are review checkpoints, not automatic validation gates — no action is taken automatically at either date.`;

      el.innerHTML = `
        <li><div class="rm-dot complete"></div><div><div class="rm-title">Historical comparison — complete</div>
          <div class="rm-detail">Offline replay against past data, a separate evaluation with its own source, dates and unknown-outcome exclusions — see research/offline_comparison/RESULTS.md. Never combined with the live figures on this page.</div></div></li>
        <li><div class="rm-dot ${{obsDot}}"></div><div><div class="rm-title">${{obsTitle}}</div>
          <div class="rm-detail">${{obsDetail}}</div></div></li>
        <li><div class="rm-dot pending"></div><div><div class="rm-title">Practice execution — pending review</div>
          <div class="rm-detail">Not started. Would require an explicit decision after the checkpoints above, not an automatic transition.</div></div></li>
        <li><div class="rm-dot pending"></div><div><div class="rm-title">Product readiness — pending</div>
          <div class="rm-detail">Not started.</div></div></li>`;
    }}

    function renderKpiSection(payload) {{
      latestKpiPayload = payload;
      renderKpiView();
    }}

    function renderKpiView() {{
      const payload = latestKpiPayload;
      const tilesC = document.getElementById('research-kpi-tiles-container');
      const freshnessEl = document.getElementById('research-kpi-freshness');
      const chartSvgEl = document.getElementById('research-chart');
      const chartEmptyEl = document.getElementById('research-chart-empty');

      const explicit = explicitStateBlock(payload);
      if (explicit) {{
        tilesC.innerHTML = explicit;
        document.getElementById('research-ledger-body').innerHTML = '<tr><td colspan="8" class="empty">No data.</td></tr>';
        document.getElementById('research-suppressed-body').innerHTML = '<tr><td colspan="4" class="empty">No data.</td></tr>';
        chartSvgEl.style.display = 'none';
        chartEmptyEl.textContent = 'No completed trades yet.';
        chartEmptyEl.style.display = 'block';
        freshnessEl.textContent = '';
        return;
      }}

      // Cross-check run identity BEFORE presenting health and KPI/ledger
      // together -- two snapshots published independently (different
      // cadences) can legitimately disagree for a short window around a
      // new run starting. Showing them side by side as if they described
      // the same run would be actively misleading, so performance/ledger
      // is withheld entirely (not partially shown, not guessed at) until
      // both publishers agree again. Rechecked by every refresh, health
      // or kpi -- see renderHealth()'s and renderKpiSection()'s own calls
      // into this function.
      const mismatch = runIdMismatch();
      if (mismatch) {{
        const genAt = payload.generated_at_utc ? new Date(payload.generated_at_utc) : null;
        const ageSec = genAt ? (Date.now() - genAt.getTime()) / 1000 : null;
        freshnessEl.innerHTML = genAt ? `<span class="snapshot-note">snapshot ${{fmtAge(ageSec)}}</span>` : '';
        tilesC.innerHTML = `<p class="empty">Run identity mismatch — the health snapshot (run ${{mismatch.healthRunId.slice(0, 12)}}…) and the
          KPI snapshot (run ${{mismatch.kpiRunId.slice(0, 12)}}…) do not agree. Withholding performance and the trade ledger until both
          publishers report the same run (this resolves itself once the slower KPI publisher catches up to a new run, or reverses if it
          was the health side that was behind).</p>`;
        document.getElementById('research-ledger-body').innerHTML = '<tr><td colspan="8" class="empty">Withheld — run identity mismatch.</td></tr>';
        document.getElementById('research-suppressed-body').innerHTML = '<tr><td colspan="4" class="empty">Withheld — run identity mismatch.</td></tr>';
        chartSvgEl.style.display = 'none';
        chartEmptyEl.textContent = 'Withheld — run identity mismatch.';
        chartEmptyEl.style.display = 'block';
        return;
      }}

      const kpi = payload.kpi, ledger = payload.ledger || [];
      const ledgerMeta = payload.ledger_meta || null;
      const genAt = payload.generated_at_utc ? new Date(payload.generated_at_utc) : null;
      const ageSec = genAt ? (Date.now() - genAt.getTime()) / 1000 : null;
      const isStale = ageSec !== null && ageSec > kpiStaleAfter;
      freshnessEl.innerHTML = genAt ? `<span class="snapshot-note ${{isStale ? 'is-stale' : ''}}">snapshot ${{fmtAge(ageSec)}}${{isStale ? ' — STALE' : ''}}</span>` : '';

      const c = kpi.counts;
      const completedAvg = kpi.avg_net_r_per_completed_trade;
      const allEligible = kpi.avg_net_r_per_all_eligible_alert;

      // "Avg R / All Eligible Alerts": three genuinely distinct states,
      // never collapsed into one another --
      //   (a) denominator=0 (no eligible alerts at all, e.g. a fresh run):
      //       value is null but is_undetermined is FALSE (report.py only
      //       sets is_undetermined when an unknown OUTCOME exists, not
      //       when there's simply nothing to compute yet) -- must still
      //       render neutral, never fall through to a pos/neg comparison
      //       against null.
      //   (b) is_undetermined (>=1 unknown outcome among real alerts).
      //   (c) a genuine finite value.
      let allEligibleText, allEligibleClass, allEligibleSub;
      if (!allEligible.denominator) {{
        allEligibleText = '—'; allEligibleClass = ''; allEligibleSub = 'No eligible alerts yet';
      }} else if (allEligible.is_undetermined || !isFiniteNum(allEligible.value)) {{
        allEligibleText = 'Undetermined'; allEligibleClass = 'undetermined';
        allEligibleSub = allEligible.reason || 'Value unavailable';
      }} else {{
        allEligibleText = fmtR2(allEligible.value); allEligibleClass = perfClass(allEligible.value);
        allEligibleSub = 'denominator=' + allEligible.denominator;
      }}
      const maxDd = kpi.max_drawdown_r_partial_completed_trades_only;
      const maxDdClass = isFiniteNum(maxDd) && maxDd < 0 ? 'neg' : '';  // never 'pos' -- a drawdown is never "positive performance"

      tilesC.innerHTML = `<div class="stat-tiles">
        <div class="tile"><div class="label">Eligible Alerts</div><div class="value">${{c.eligible_alerts}}</div><div class="sub">${{c.suppressed_existing_position}} suppressed (not counted)</div></div>
        <div class="tile"><div class="label">Entered / Completed</div><div class="value">${{c.entered}} / ${{c.completed}}</div><div class="sub">${{c.pending_open}} pending · ${{c.unknown_total}} unknown · ${{c.missed_entries_confirmed_zero_pnl}} missed (0)</div></div>
        <div class="tile"><div class="label">Avg R / Completed Trade</div><div class="value ${{perfClass(completedAvg.value)}}">${{fmtR2(completedAvg.value)}}</div><div class="sub">n=${{completedAvg.denominator}}</div></div>
        <div class="tile"><div class="label">Avg R / All Eligible Alerts</div><div class="value ${{allEligibleClass}}">${{allEligibleText}}</div><div class="sub">${{allEligibleSub}}</div></div>
        <div class="tile"><div class="label">Max Drawdown</div><div class="value ${{maxDdClass}}">${{fmtR2(maxDd)}}</div><div class="sub">completed trades only${{c.unknown_total > 0 ? ' — PARTIAL, unknown outcomes exist' : ''}}</div></div>
      </div>`;

      // ---- chart: cumulative completed-trade R, with a drawdown shade, axes labelled ----
      const series = kpi.equity_curve || [];
      const chartSvg = document.getElementById('research-chart');
      const chartEmpty = document.getElementById('research-chart-empty');
      const partialNote = document.getElementById('research-chart-partial-note');
      partialNote.textContent = c.unknown_total > 0
        ? `PARTIAL — ${{c.unknown_total}} alert(s) with an unknown outcome are excluded from this chart.` : '';
      if (!series.length) {{
        chartSvg.style.display = 'none';
        chartEmpty.textContent = 'No completed trades yet.';  // resets any earlier withheld/mismatch message
        chartEmpty.style.display = 'block';
        document.getElementById('research-chart-cur').textContent = '';
      }} else {{
        chartSvg.style.display = 'block';
        chartEmpty.style.display = 'none';
        const W = 640, H = 220, padL = 34, padR = 8, padT = 10, padB = 20;
        const values = series.map(p => p.cumulative_r);
        const ddValues = series.map(p => p.drawdown_r);
        const max = Math.max(0, ...values), min = Math.min(0, ...ddValues, ...values);
        const span = (max - min) || 1;
        const n = series.length;
        const xs = i => padL + (n <= 1 ? (W - padL - padR) / 2 : (i / (n - 1)) * (W - padL - padR));
        const ys = v => H - padB - ((v - min) / span) * (H - padT - padB);
        const linePts = series.map((p, i) => `${{xs(i)}},${{ys(p.cumulative_r)}}`).join(' ');
        const areaPts = linePts + ` ${{xs(n - 1)}},${{ys(0)}} ${{xs(0)}},${{ys(0)}}`;
        const ddPts = series.map((p, i) => `${{xs(i)}},${{ys(p.drawdown_r)}}`).join(' ');
        const last = series[n - 1];
        const lineColor = last.cumulative_r >= 0 ? 'var(--long)' : 'var(--short)';
        const firstDate = series[0].exit_time_utc ? series[0].exit_time_utc.slice(0, 10) : '';
        const lastDate = last.exit_time_utc ? last.exit_time_utc.slice(0, 10) : '';
        chartSvg.innerHTML = `
          <defs><linearGradient id="researchAreaFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="${{lineColor}}" stop-opacity="0.26" /><stop offset="100%" stop-color="${{lineColor}}" stop-opacity="0" />
          </linearGradient></defs>
          <text x="2" y="${{H - 4}}" class="axis-label" fill="var(--text-faint)" font-size="9.5">${{min.toFixed(1)}}R</text>
          <text x="2" y="${{padT + 9}}" class="axis-label" fill="var(--text-faint)" font-size="9.5">${{max.toFixed(1)}}R</text>
          <text x="${{padL}}" y="${{H - 4}}" class="axis-label" fill="var(--text-faint)" font-size="9.5">${{firstDate}}</text>
          <text x="${{W - padR}}" y="${{H - 4}}" text-anchor="end" class="axis-label" fill="var(--text-faint)" font-size="9.5">${{lastDate}}</text>
          <line x1="${{padL}}" y1="${{ys(0)}}" x2="${{W-padR}}" y2="${{ys(0)}}" stroke="var(--border)" stroke-width="1" />
          <polyline points="${{ddPts}}" fill="none" stroke="var(--short)" stroke-width="1.3" stroke-dasharray="3,2" opacity="0.8" />
          <polygon points="${{areaPts}}" fill="url(#researchAreaFill)" />
          <polyline points="${{linePts}}" fill="none" stroke="${{lineColor}}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
          <circle cx="${{xs(n - 1)}}" cy="${{ys(last.cumulative_r)}}" r="4" fill="${{lineColor}}" />`;
        document.getElementById('research-chart-cur').textContent = fmtR2(last.cumulative_r);
      }}

      // ---- ledger + suppressed tables ----
      const STATE_LABEL = {{
        stopped: 'Stopped', targeted: 'Targeted', time_exited: 'Time exit', expired_no_entry: 'Missed (expired)',
        ambiguous_intrabar_exit: 'Ambiguous exit', insufficient_data_entry: 'Insufficient data', incomplete_coverage: 'Incomplete coverage',
        open: 'Open', actionable_open: 'Actionable (open)', suppressed_existing_position: 'Suppressed',
      }};
      const RESOLVED = new Set(['stopped', 'targeted', 'time_exited']);
      const executable = ledger.filter(r => r.executable);
      const suppressed = ledger.filter(r => !r.executable);
      const ledgerScope = document.getElementById('research-ledger-scope');
      ledgerScope.textContent = ledgerMeta && ledgerMeta.truncated
        ? `Most recent ${{ledgerMeta.published_rows}} of ${{ledgerMeta.total_rows}} decisions shown; aggregate KPIs cover all eligible decisions.`
        : `All ${{ledgerMeta ? ledgerMeta.total_rows : ledger.length}} recorded decisions shown; aggregate KPIs cover all eligible decisions.`;

      const ledgerBody = document.getElementById('research-ledger-body');
      if (!executable.length) {{
        ledgerBody.innerHTML = '<tr><td colspan="8" class="empty">No eligible alerts recorded yet.</td></tr>';
      }} else {{
        ledgerBody.innerHTML = executable.map((r, i) => {{
          const badgeKind = RESOLVED.has(r.state) ? (r.r_multiple >= 0 ? 'outcome-target' : 'outcome-stop') : 'outcome-unresolved';
          const entryStr = r.assumed_entry_time_utc ? `${{r.assumed_entry_price.toFixed(5)}}<br><span class="hint">${{fmtUTC2(r.assumed_entry_time_utc)}}</span>` : '—';
          const exitStr = r.exit_time_utc ? `${{r.exit_price.toFixed(5)}}<br><span class="hint">${{fmtUTC2(r.exit_time_utc)}}</span>` : '—';
          const rowId = `ledger-row-${{i}}`;
          return `<tr class="ledger-row" data-target="${{rowId}}">
              <td><span class="chevron">▸</span></td>
              <td class="pair">${{r.pair}}</td>
              <td><span class="badge ${{r.direction}}">${{r.direction.toUpperCase()}}</span></td>
              <td class="num">${{r.entry_condition_lo.toFixed(5)}}–${{r.entry_condition_hi.toFixed(5)}}<br><span class="hint">SL ${{r.stop.toFixed(5)}} / TP ${{r.target.toFixed(5)}}</span></td>
              <td class="num">${{entryStr}}</td>
              <td class="num">${{exitStr}}</td>
              <td><span class="badge ${{badgeKind}}">${{STATE_LABEL[r.state] || r.state}}</span>${{!RESOLVED.has(r.state) ? `<br><span class="hint reason-text">${{STATE_LABEL[r.state] || r.state}}</span>` : ''}}</td>
              <td class="num">${{fmtR2(r.r_multiple)}}</td>
            </tr>
            <tr class="ledger-detail-row" id="${{rowId}}" style="display:none"><td colspan="8"><dl>
              <div><dt>Source candle completion</dt><dd>${{fmtUTC2(r.source_candle_completion_utc)}}</dd></div>
              <div><dt>Recorded</dt><dd>${{fmtUTC2(r.actual_recording_time_utc)}}</dd></div>
              <div><dt>Decision delay</dt><dd>${{r.decision_delay_seconds !== null && r.decision_delay_seconds !== undefined ? r.decision_delay_seconds.toFixed(1) + 's' : '—'}}</dd></div>
              <div><dt>Entry expiry</dt><dd>${{fmtUTC2(r.entry_expiry_utc)}}</dd></div>
              <div><dt>Max holding</dt><dd>${{r.max_holding_time_hours}}h</dd></div>
              <div><dt>Scheduled exit (deadline)</dt><dd>${{fmtUTC2(r.scheduled_exit_time_utc)}}</dd></div>
              <div><dt>Execution delay</dt><dd>${{r.execution_delay_seconds !== null && r.execution_delay_seconds !== undefined ? r.execution_delay_seconds.toFixed(1) + 's' : '—'}}</dd></div>
              <div><dt>Same-sample exit</dt><dd>${{r.same_sample_exit ? 'yes' : 'no'}}</dd></div>
              <div style="grid-column:1/-1"><dt>Caveats</dt><dd style="font-family:var(--font-body);font-size:11.5px">${{(r.caveats || []).join(' ')}}</dd></div>
            </dl></td></tr>`;
        }}).join('');
        ledgerBody.querySelectorAll('.ledger-row').forEach(row => {{
          row.addEventListener('click', () => {{
            const detail = document.getElementById(row.dataset.target);
            const show = detail.style.display === 'none';
            detail.style.display = show ? 'table-row' : 'none';
            row.classList.toggle('expanded', show);
          }});
        }});
      }}

      const suppBody = document.getElementById('research-suppressed-body');
      suppBody.innerHTML = suppressed.length
        ? suppressed.map(r => `<tr><td class="pair">${{r.pair}}</td><td><span class="badge ${{r.direction}}">${{r.direction.toUpperCase()}}</span></td>
            <td class="mono">${{fmtUTC2(r.actual_recording_time_utc)}}</td><td class="mono">${{fmtUTC2(r.suppressed_until_utc)}}</td></tr>`).join('')
        : '<tr><td colspan="4" class="empty">None.</td></tr>';
    }}

    function refreshHealth() {{
      fetch('/research/health', {{ credentials: 'same-origin' }}).then(r => r.json()).then(renderHealth).catch(() => {{}});
    }}
    function refreshKpi() {{
      const btn = document.getElementById('research-kpi-refresh');
      btn.disabled = true;
      fetch('/research/kpi', {{ credentials: 'same-origin' }}).then(r => r.json()).then(k => {{ renderKpiSection(k); }})
        .catch(() => {{}}).finally(() => {{ btn.disabled = false; }});
    }}

    document.getElementById('research-kpi-refresh').addEventListener('click', refreshKpi);
    renderHealth(initialResearch.health);
    renderKpiSection(initialResearch.kpi);
    setInterval(refreshHealth, 30000);   // health: frequent, independent of the heavier KPI calculation
    setInterval(refreshKpi, 300000);     // kpi/ledger: much less frequent (real publish cadence is 15-30 min)
  }}
</script>
</body></html>"""


def load_alerts() -> dict:
    if not ALERTS_PATH.exists():
        return {"alerts": []}
    try:
        return json.loads(ALERTS_PATH.read_text())
    except json.JSONDecodeError:
        return {"alerts": []}


def load_research_snapshot(path: Path) -> dict:
    """Reads a pre-computed research_snapshot_publisher.py output file
    as-is -- no scoring, no log scanning, no systemctl, just a file read.
    Always returns a dict with a `state` key so the template never has to
    special-case None: "not_published" (file doesn't exist -- the
    publisher hasn't run yet, e.g. right after this branch first deploys)
    and "malformed_snapshot_file" (exists but isn't valid JSON -- a
    corrupt/truncated write, distinct from the publisher's own explicit
    states like observer_not_found/no_manifest/manifest_mismatch/error,
    which are already present in a well-formed file)."""
    if not path.exists():
        return {"state": "not_published", "error": None, "generated_at_utc": None}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"state": "malformed_snapshot_file", "error": None, "generated_at_utc": None}


@app.get("/", response_class=HTMLResponse)
def dashboard(_: None = Depends(check_auth)) -> str:
    live_payload = None
    if LIVE_SCAN_PATH.exists():
        live_payload = json.loads(LIVE_SCAN_PATH.read_text())
    performance_payload = None
    if PERFORMANCE_PATH.exists():
        try:
            performance_payload = json.loads(PERFORMANCE_PATH.read_text())
        except json.JSONDecodeError:
            performance_payload = None
    research_health_payload = load_research_snapshot(RESEARCH_HEALTH_PATH)
    research_kpi_payload = load_research_snapshot(RESEARCH_KPI_PATH)
    research_health_v2_payload = load_research_snapshot(RESEARCH_HEALTH_V2_PATH)
    research_kpi_v2_payload = load_research_snapshot(RESEARCH_KPI_V2_PATH)
    return render_page(live_payload, performance_payload, load_alerts(), research_health_payload, research_kpi_payload,
                       research_health_v2_payload, research_kpi_v2_payload)


@app.get("/table", response_class=HTMLResponse)
def table_partial(_: None = Depends(check_auth)) -> str:
    payload = None
    if LIVE_SCAN_PATH.exists():
        payload = json.loads(LIVE_SCAN_PATH.read_text())
    return render_table_body(payload)


@app.get("/feed", response_class=HTMLResponse)
def feed_partial(_: None = Depends(check_auth)) -> str:
    return render_feed_body(load_alerts())


@app.get("/alerts", response_class=JSONResponse)
def alerts(_: None = Depends(check_auth)) -> dict:
    return load_alerts()


@app.get("/research/health", response_class=JSONResponse)
def research_health(_: None = Depends(check_auth)) -> dict:
    """Reads research_health.json as-is -- no OANDA call, no scoring, no
    log scan, no systemctl here; all of that already happened in the
    separate research_snapshot_publisher.py process. Polled frequently by
    the Research view's client-side auto-refresh."""
    return load_research_snapshot(RESEARCH_HEALTH_PATH)


@app.get("/research/kpi", response_class=JSONResponse)
def research_kpi(_: None = Depends(check_auth)) -> dict:
    """Reads research_kpi.json as-is -- same read-only, pre-computed-only
    discipline as /research/health. Used for the Research view's manual
    Refresh button (the underlying KPI/ledger calculation is deliberately
    much less frequent than health, see research_snapshot_publisher.py)."""
    return load_research_snapshot(RESEARCH_KPI_PATH)


@app.get("/research/health_v2", response_class=JSONResponse)
def research_health_v2(_: None = Depends(check_auth)) -> dict:
    """v2 measurement contract -- reads research_health_v2.json, a
    COMPLETELY SEPARATE file from v1's, written only by a publisher
    invocation pointed at a v2 observer checkout. See
    research/prospective_baseline_v2/MEASUREMENT_CONTRACT.md."""
    return load_research_snapshot(RESEARCH_HEALTH_V2_PATH)


@app.get("/research/kpi_v2", response_class=JSONResponse)
def research_kpi_v2(_: None = Depends(check_auth)) -> dict:
    """v2 measurement contract -- reads research_kpi_v2.json. Never the
    same file, never blended with v1's /research/kpi."""
    return load_research_snapshot(RESEARCH_KPI_V2_PATH)


@app.get("/api/performance", response_class=JSONResponse)
def performance(_: None = Depends(check_auth_or_monitor_key)) -> dict:
    if not PERFORMANCE_PATH.exists():
        return {"updated_at": None, "signals": [], "aggregates": {}}
    try:
        return json.loads(PERFORMANCE_PATH.read_text())
    except json.JSONDecodeError:
        return {"updated_at": None, "signals": [], "aggregates": {}}


@app.get("/api/health", response_class=JSONResponse)
def health(_: None = Depends(check_auth_or_monitor_key)) -> dict:
    """Purpose-built for automated monitoring (e.g. a scheduled daily check)
    rather than having a script scrape HTML for the "last scan" line. Judge
    freshness against each writer's own cadence: live_scan_age_seconds
    should be well under 60s (streaming_scanner recomputes every ~30s);
    performance_updated_at should be within the last couple of hours
    (performance_scorer runs hourly)."""
    now = datetime.now(timezone.utc)
    live_scan_age_seconds = None
    if LIVE_SCAN_PATH.exists():
        try:
            payload = json.loads(LIVE_SCAN_PATH.read_text())
            generated = datetime.fromisoformat(payload["generated_at"])
            live_scan_age_seconds = (now - generated).total_seconds()
        except (json.JSONDecodeError, KeyError, ValueError):
            live_scan_age_seconds = None
    performance_updated_at = None
    if PERFORMANCE_PATH.exists():
        try:
            performance_updated_at = json.loads(PERFORMANCE_PATH.read_text()).get("updated_at")
        except json.JSONDecodeError:
            performance_updated_at = None
    alerts = load_alerts().get("alerts", [])
    return {
        "checked_at": now.isoformat(),
        "live_scan_age_seconds": live_scan_age_seconds,
        "performance_updated_at": performance_updated_at,
        "alerts_stored_count": len(alerts),
        "most_recent_alert": alerts[-1] if alerts else None,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
