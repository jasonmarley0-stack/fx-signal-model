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
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

LIVE_SCAN_PATH = Path(__file__).parent / "live_scan.json"
ALERTS_PATH = Path(__file__).parent / "alerts.json"
PERFORMANCE_PATH = Path(__file__).parent / "performance.json"
app = FastAPI()
security = HTTPBasic()


def check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    user = os.environ.get("DASHBOARD_USER", "")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    ok_user = secrets.compare_digest(credentials.username, user)
    ok_pass = secrets.compare_digest(credentials.password, password)
    if not (user and password and ok_user and ok_pass):
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


def render_page(live_payload: dict | None, performance_payload: dict | None, alerts_payload: dict | None) -> str:
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
  </main>
  <nav class="bottom-nav" id="bottom-nav">
    <button class="active" data-view="live">Live</button>
    <button data-view="performance">Performance</button>
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
</script>
</body></html>"""


def load_alerts() -> dict:
    if not ALERTS_PATH.exists():
        return {"alerts": []}
    try:
        return json.loads(ALERTS_PATH.read_text())
    except json.JSONDecodeError:
        return {"alerts": []}


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
    return render_page(live_payload, performance_payload, load_alerts())


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


@app.get("/api/performance", response_class=JSONResponse)
def performance(_: None = Depends(check_auth)) -> dict:
    if not PERFORMANCE_PATH.exists():
        return {"updated_at": None, "signals": [], "aggregates": {}}
    try:
        return json.loads(PERFORMANCE_PATH.read_text())
    except json.JSONDecodeError:
        return {"updated_at": None, "signals": [], "aggregates": {}}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
