"""Tiny live dashboard for the droplet: reads live_scan.json (written by
scanner_oanda.py every 30 min) and renders it as a password-protected web
page. Deliberately does NOT call OANDA or Signal Engine itself — it only
ever reads the snapshot file, so this page can be slow, crash, or get
hammered by refreshes without ever affecting the scanner or hitting rate
limits on either upstream API.

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
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

LIVE_SCAN_PATH = Path(__file__).parent / "live_scan.json"
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


def row_html(row: dict) -> str:
    pair = row["pair"]
    if "error" in row:
        return f"""
        <tr class="err-row">
          <td>{pair}</td><td colspan="6" class="err">{row['error']}</td>
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

    return f"""
    <tr>
      <td class="pair">{pair}</td>
      <td class="num">{fmt_price(row['entry'])}</td>
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
    <tr class="reason-row"><td></td><td colspan="6" class="reason">{row['reason']}{window_str}</td></tr>"""


def render_page(payload: dict | None) -> str:
    if payload is None:
        body = '<p class="empty">No scan data yet — the scanner hasn\'t completed its first pass. Check back in a few minutes.</p>'
        generated_str = "—"
    else:
        generated = datetime.fromisoformat(payload["generated_at"])
        age_min = (datetime.now(timezone.utc) - generated).total_seconds() / 60
        generated_str = f"{generated.strftime('%Y-%m-%d %H:%M UTC')} ({age_min:.0f} min ago)"
        rows_html = "".join(row_html(r) for r in payload["rows"])
        body = f"""
        <table>
          <thead><tr>
            <th>Pair</th><th>Price</th><th>Technical</th><th>PESTLE</th>
            <th>Combined</th><th>Signal</th><th>SL / TP</th>
          </tr></thead>
          <tbody>{rows_html}</tbody>
        </table>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/>
<meta http-equiv="refresh" content="60"/>
<title>FX Signal Scanner — Live</title>
<style>
  :root {{
    --bg:#0d0f12; --panel:#15181c; --border:rgba(255,255,255,0.08);
    --text:#e8e9ea; --muted:#8b9098; --accent:#4a9eff;
    --long:#3ecf7e; --short:#f0654a; --neutral:#8b9098;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:var(--bg); color:var(--text); margin:0; padding:32px 20px 64px;
  }}
  .wrap {{ max-width:1100px; margin:0 auto; }}
  h1 {{ font-size:20px; margin:0 0 4px; font-weight:600; }}
  .subtitle {{ color:var(--muted); font-size:13px; margin-bottom:24px; font-variant-numeric:tabular-nums; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; background:var(--panel);
    border:1px solid var(--border); border-radius:10px; overflow:hidden; }}
  th, td {{ padding:10px 12px; text-align:left; border-bottom:1px solid var(--border); vertical-align:top; }}
  th {{ background:rgba(255,255,255,0.03); font-size:11px; text-transform:uppercase;
    letter-spacing:0.04em; color:var(--muted); font-weight:600; }}
  .pair {{ font-weight:600; }}
  .num {{ font-variant-numeric:tabular-nums; }}
  .tech-breakdown {{ color:var(--muted); font-size:12px; }}
  .composite {{ color:var(--text); font-weight:600; margin-top:2px; }}
  .combined {{ font-weight:700; font-size:14px; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px;
    font-weight:700; letter-spacing:0.03em; }}
  .badge.long {{ background:rgba(62,207,126,0.15); color:var(--long); }}
  .badge.short {{ background:rgba(240,101,74,0.15); color:var(--short); }}
  .badge.neutral {{ background:rgba(139,144,152,0.15); color:var(--neutral); }}
  .conf {{ color:var(--muted); font-size:11px; text-transform:uppercase; }}
  .sltp {{ font-size:12px; line-height:1.6; }}
  .reason-row td {{ border-bottom:1px solid var(--border); padding-top:0; }}
  .reason {{ color:var(--muted); font-size:12px; }}
  .window {{ color:var(--accent); font-size:11px; margin-top:2px; }}
  .err-row .err {{ color:var(--short); font-size:12px; }}
  .empty {{ color:var(--muted); }}
</style></head><body><div class="wrap">
  <h1>FX Signal Scanner</h1>
  <div class="subtitle">Last scan: {generated_str} · auto-refreshes every 60s</div>
  {body}
</div></body></html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard(_: None = Depends(check_auth)) -> str:
    payload = None
    if LIVE_SCAN_PATH.exists():
        payload = json.loads(LIVE_SCAN_PATH.read_text())
    return render_page(payload)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
