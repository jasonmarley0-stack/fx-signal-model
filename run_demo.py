"""End-to-end smoke test: synthetic OHLC -> backtest -> sample dashboard.

Run: python run_demo.py
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from backtest import synthetic_ohlc, run_backtest
from dashboard_data import build_dashboard_payload, DEFAULT_PAIRS
from combiner import TradeSignal

sys.path.insert(0, str(Path(__file__).parent / "dashboard"))
from generate_dashboard import render_dashboard


def main():
    print("=== 1. Backtest (synthetic data, technical layer only) ===")
    df = synthetic_ohlc(n_bars=3000)
    result = run_backtest(df)
    print(f"Trades: {result.total_trades}")
    print(f"Win rate: {result.win_rate:.1%}")
    print(f"Expectancy: {result.expectancy_r:+.2f}R per trade")
    print(f"Max drawdown: {result.max_drawdown_r:.2f}R")
    print("NOTE: synthetic data has no real market structure — this only proves")
    print("the pipeline runs end to end, not that the strategy has edge.\n")

    print("=== 2. Morning dashboard (mock PESTLE + synthetic technical) ===")
    pair_dfs = {pair: synthetic_ohlc(n_bars=200, seed=hash(pair) % 1000) for pair in DEFAULT_PAIRS}
    payload = build_dashboard_payload(pair_dfs)

    # serialize TradeSignal dataclasses for the renderer/JSON dump
    def serialize(row):
        sig = row["signal"]
        row = dict(row)
        window = None
        if sig.window:
            window = dict(sig.window)
            window["generated_at_utc"] = window["generated_at_utc"].isoformat()
            window["valid_until_utc"] = window["valid_until_utc"].isoformat()
        row["signal"] = {
            "direction": sig.direction, "confidence": sig.confidence,
            "combined_score": sig.combined_score, "entry": sig.entry,
            "stop_loss_range": list(sig.stop_loss_range),
            "take_profit_range": list(sig.take_profit_range), "reason": sig.reason,
            "window": window,
        }
        return row

    payload["rows"] = [serialize(r) for r in payload["rows"]]
    Path("dashboard_payload.json").write_text(json.dumps(payload, indent=2, default=str))

    html = render_dashboard(payload)
    Path("morning_dashboard.html").write_text(html)
    print(f"Built dashboard for {len(payload['rows'])} pairs -> morning_dashboard.html")


if __name__ == "__main__":
    main()
