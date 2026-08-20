"""Renders the morning dashboard payload (from src/dashboard_data.py) into a
self-contained HTML file. Palette/marks follow the dataviz skill defaults.
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>FX Morning Signal Dashboard</title>
<style>
  .viz-root {{
    color-scheme: light;
    --surface-1: #fcfcfb; --page: #f9f9f7;
    --text-primary: #0b0b0b; --text-secondary: #52514e; --text-muted: #898781;
    --gridline: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
    --blue: #2a78d6; --orange: #eb6834; --aqua: #1baf7a; --yellow: #eda100;
    --magenta: #e87ba4; --green: #008300; --violet: #4a3aa7; --red: #e34948;
    --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:where(:not([data-theme="light"])) .viz-root {{
      color-scheme: dark;
      --surface-1: #1a1a19; --page: #0d0d0d;
      --text-primary: #ffffff; --text-secondary: #c3c2b7; --text-muted: #898781;
      --gridline: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
      --blue: #3987e5; --orange: #d95926; --aqua: #199e70; --yellow: #c98500;
      --magenta: #d55181; --green: #008300; --violet: #9085e9; --red: #e66767;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family: system-ui,-apple-system,"Segoe UI",sans-serif; background: var(--page); color: var(--text-primary); }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 32px 20px 64px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .subtitle {{ color: var(--text-secondary); font-size: 13px; margin-bottom: 28px; }}
  .disclaimer {{ font-size: 12px; color: var(--text-muted); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; margin-bottom: 24px; background: var(--surface-1); }}
  .card {{ background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 20px 22px; margin-bottom: 16px; }}
  .card-head {{ display:flex; align-items:center; justify-content:space-between; margin-bottom: 14px; flex-wrap: wrap; gap: 8px; }}
  .pair {{ font-size: 18px; font-weight: 600; }}
  .badge {{ font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 999px; display:inline-flex; align-items:center; gap:6px; }}
  .badge.long {{ background: color-mix(in srgb, var(--good) 16%, transparent); color: var(--good); }}
  .badge.short {{ background: color-mix(in srgb, var(--critical) 16%, transparent); color: var(--critical); }}
  .badge.no_trade {{ background: color-mix(in srgb, var(--text-muted) 18%, transparent); color: var(--text-secondary); }}
  .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  @media (max-width: 720px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .row {{ display:flex; align-items:center; gap: 10px; font-size: 13px; margin: 6px 0; }}
  .row .label {{ width: 130px; color: var(--text-secondary); flex-shrink:0; }}
  .bar-track {{ flex:1; height: 10px; border-radius: 5px; background: var(--gridline); position: relative; overflow:hidden; }}
  .bar-fill {{ position:absolute; top:0; bottom:0; border-radius: 5px; }}
  .bar-fill.pos {{ background: var(--blue); left: 50%; }}
  .bar-fill.neg {{ background: var(--red); right: 50%; }}
  .mid {{ position:absolute; left:50%; top:-2px; bottom:-2px; width:1px; background: var(--baseline); }}
  .score-val {{ width: 44px; text-align:right; font-variant-numeric: tabular-nums; color: var(--text-primary); font-size: 12px; }}
  .sltp {{ display:flex; gap: 24px; margin-top: 14px; font-size: 13px; }}
  .sltp .k {{ color: var(--text-secondary); }}
  .sltp .v {{ font-variant-numeric: tabular-nums; font-weight: 600; }}
  .reason {{ font-size: 12px; color: var(--text-muted); margin-top: 10px; }}
  .narrative {{ margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border); }}
  .narrative-item {{ font-size: 12px; color: var(--text-secondary); line-height: 1.5; margin: 4px 0; }}
  .narrative-item .k {{ color: var(--text-muted); font-weight: 600; margin-right: 4px; }}
  .window {{ font-size: 12px; color: var(--text-secondary); margin-top: 12px; display:flex; align-items:center; gap:8px; }}
  .upcoming {{ font-size: 12px; color: var(--text-secondary); margin-top: 10px; }}
  .upcoming .pill {{ font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 999px; background: color-mix(in srgb, var(--yellow) 18%, transparent); color: var(--yellow); margin-right: 6px; }}
  .window .pill {{ font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 999px; background: color-mix(in srgb, var(--blue) 14%, transparent); color: var(--blue); }}
  .window .stale {{ background: color-mix(in srgb, var(--critical) 14%, transparent); color: var(--critical); }}
  .section-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--text-muted); margin: 4px 0 8px; }}
  table.legend-table {{ width:100%; border-collapse: collapse; font-size: 12px; }}
  table.legend-table td {{ padding: 3px 6px; color: var(--text-secondary); }}
</style>
</head>
<body>
<div class="viz-root">
<div class="wrap">
  <h1>FX Morning Signal Dashboard</h1>
  <div class="subtitle">Generated {generated_at}</div>
  <div class="disclaimer">Decision-support output only, not financial advice. Technical + PESTLE scores are rule-based heuristics, not calibrated probabilities — validate against a live backtest and paper-trade before risking capital.</div>
  {cards}
</div>
</div>
</body>
</html>
"""

CARD_TEMPLATE = """
<div class="card">
  <div class="card-head">
    <div class="pair">{pair}</div>
    <div class="badge {direction}">{direction_label} · {confidence} confidence</div>
  </div>
  <div class="grid">
    <div>
      <div class="section-label">Technical (ORB / Trend / Pattern)</div>
      {tech_rows}
      {composite_row}
    </div>
    <div>
      <div class="section-label">PESTLE ({base}−{quote})</div>
      {pestle_rows}
      {pestle_composite_row}
    </div>
  </div>
  <div class="sltp">
    <div><span class="k">Entry</span> <span class="v">{entry:.5f}</span></div>
    <div><span class="k">Stop-loss</span> <span class="v">{sl_lo:.5f} – {sl_hi:.5f}</span></div>
    <div><span class="k">Take-profit</span> <span class="v">{tp_lo:.5f} – {tp_hi:.5f}</span></div>
  </div>
  <div class="window">{window_html}</div>
  {upcoming_html}
  <div class="narrative">
    <div class="narrative-item"><span class="k">Technical:</span> {narrative_technical}</div>
    <div class="narrative-item"><span class="k">{base} PESTLE:</span> {narrative_pestle_base}</div>
    <div class="narrative-item"><span class="k">{quote} PESTLE:</span> {narrative_pestle_quote}</div>
  </div>
  <div class="reason">{reason}</div>
</div>
"""


def _score_row(label, score):
    score = max(-1.0, min(1.0, score))
    cls = "pos" if score >= 0 else "neg"
    width = abs(score) * 50
    style = f"width:{width:.1f}%;" + ("left:50%;" if cls == "pos" else f"right:50%;")
    return (
        f'<div class="row"><div class="label">{label}</div>'
        f'<div class="bar-track"><div class="mid"></div><div class="bar-fill {cls}" style="{style}"></div></div>'
        f'<div class="score-val">{score:+.2f}</div></div>'
    )


def _format_generated_at(generated_at) -> str:
    """generated_at is a UTC ISO string (see dashboard_data.py) — label it
    explicitly as GMT so it isn't mistaken for local time (the UK is on BST,
    UTC+1, roughly late March to late October)."""
    if isinstance(generated_at, str):
        try:
            dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            return dt.strftime("%H:%M GMT, %d %b %Y")
        except ValueError:
            pass
    return f"{generated_at} (GMT)"


def render_dashboard(payload: dict) -> str:
    cards = []
    for row in payload["rows"]:
        sig = row["signal"]
        tech = row["tech"]
        pestle = row["pestle"]
        tech_rows = "".join([
            _score_row("Opening Range BO", tech["orb"]),
            _score_row("Trend/Momentum", tech["trend"]),
            _score_row("Candlestick", tech["pattern"]),
        ])
        composite_row = _score_row("Composite", tech["composite"])
        pestle_rows = "".join([
            _score_row(f"{pestle['base']['currency']}", pestle["base"]["score"]),
            _score_row(f"{pestle['quote']['currency']}", pestle["quote"]["score"]),
        ])
        pestle_composite_row = _score_row("Pair PESTLE", pestle["pestle_score"])

        direction = sig.direction if hasattr(sig, "direction") else sig["direction"]
        confidence = sig.confidence if hasattr(sig, "confidence") else sig["confidence"]
        reason = sig.reason if hasattr(sig, "reason") else sig["reason"]
        entry = sig.entry if hasattr(sig, "entry") else sig["entry"]
        sl = sig.stop_loss_range if hasattr(sig, "stop_loss_range") else sig["stop_loss_range"]
        tp = sig.take_profit_range if hasattr(sig, "take_profit_range") else sig["take_profit_range"]

        direction_label = {"long": "LONG", "short": "SHORT", "no_trade": "NO TRADE"}[direction]

        window = sig.window if hasattr(sig, "window") else sig.get("window")
        window_html = ""
        if window:
            valid_until = window.get("valid_until_utc")
            is_stale = False
            if isinstance(valid_until, str):
                try:
                    valid_until_dt = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
                    is_stale = datetime.now(timezone.utc) > valid_until_dt
                except ValueError:
                    pass
            pill_class = "pill stale" if is_stale else "pill"
            pill_text = "EXPIRED" if is_stale else "LIVE"
            window_html = (
                f'<span class="{pill_class}">{pill_text}</span>'
                f'Signal window: {window["generated_at_gmt_str"]} – {window["valid_until_gmt_str"]} '
                f'({window["session"]} session)'
            )

        narrative = row.get("narrative", {})

        upcoming_html = ""
        for ev in row.get("upcoming", []):
            try:
                ev_dt = datetime.fromisoformat(ev["scheduled_at_utc"].replace("Z", "+00:00"))
                ev_str = ev_dt.strftime("%H:%M GMT, %d %b")
            except (ValueError, KeyError):
                ev_str = ev.get("scheduled_at_utc", "")
            upcoming_html += (
                f'<div class="upcoming"><span class="pill">UPCOMING</span>'
                f'{ev["currency"]}: {ev["title"]} — {ev_str}</div>'
            )

        cards.append(CARD_TEMPLATE.format(
            pair=row["pair"], direction=direction, direction_label=direction_label,
            confidence=confidence.capitalize(), tech_rows=tech_rows, composite_row=composite_row,
            base=pestle["base"]["currency"], quote=pestle["quote"]["currency"],
            pestle_rows=pestle_rows, pestle_composite_row=pestle_composite_row,
            entry=entry, sl_lo=min(sl), sl_hi=max(sl), tp_lo=min(tp), tp_hi=max(tp),
            window_html=window_html, reason=reason,
            narrative_technical=narrative.get("technical", ""),
            narrative_pestle_base=narrative.get("pestle_base", ""),
            narrative_pestle_quote=narrative.get("pestle_quote", ""),
            upcoming_html=upcoming_html,
        ))

    return TEMPLATE.format(generated_at=_format_generated_at(payload["generated_at"]), cards="".join(cards))


if __name__ == "__main__":
    payload_path = sys.argv[1] if len(sys.argv) > 1 else "dashboard_payload.json"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "morning_dashboard.html"
    payload = json.loads(Path(payload_path).read_text())
    Path(out_path).write_text(render_dashboard(payload))
    print(f"Wrote {out_path}")
