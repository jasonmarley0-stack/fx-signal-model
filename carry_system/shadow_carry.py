"""Shadow/forward test for the JPY-crosses carry strategy validated
2026-09-25 (backtest_jpy_crosses.py: cost-adjusted CAGR +2.80%, Sharpe
0.44 over ~20 years). Separate from both the FX signal product and the
backtests — this tracks what the strategy actually does going forward, on
real live prices and real live financing rates, before any capital
decision.

Carry is a standing-position strategy, not a discrete-trade one (unlike
the FX signal product) — there's no "fired signal" to score against a
future outcome. Instead this is a daily mark-to-market ledger: each day,
compute the return actually earned by whichever position was held
yesterday (price move + accrued financing), exactly matching the
backtest's methodology and no-lookahead convention. Uses OANDA's own live
financing rates directly (not the FRED history used for backtesting —
going forward, OANDA's live rate is the authoritative, directly-relevant
number: it's literally what a real account earns/pays).

Usage:
    python3 shadow_carry.py

Meant to run once daily under systemd (see
setup/shadow-carry.service + .timer), not interactively.
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "trend_system"))

import os  # noqa: E402
from oandapyV20 import API  # noqa: E402
from oandapyV20.endpoints.accounts import AccountInstruments  # noqa: E402

import pandas as pd  # noqa: E402
from backtest import fetch_daily, atr, stats, TARGET_DAILY_VOL, MAX_POSITION_WEIGHT  # noqa: E402

PAIRS = ["USD_JPY", "AUD_JPY", "NZD_JPY", "GBP_JPY", "EUR_JPY"]
SPREADS = {"USD_JPY": 0.057000, "AUD_JPY": 0.030000, "NZD_JPY": 0.035000,
           "GBP_JPY": 0.222000, "EUR_JPY": 0.152000}

STATE_PATH = Path(__file__).parent / ".shadow_carry_state.json"
LEDGER_PATH = Path(__file__).parent / "shadow_carry_ledger.jsonl"
PERFORMANCE_PATH = Path(__file__).parent / "shadow_carry_performance.json"


def client() -> API:
    return API(access_token=os.environ["OANDA_API_KEY"], environment=os.environ.get("OANDA_ENVIRONMENT", "practice"))


def live_rate_and_direction(instrument: str) -> tuple[int, float]:
    """Returns (direction, annual_rate) for whichever side currently earns
    carry — same convention as backtest_real_rates.py/backtest.py (carry
    v1), but from OANDA's live rate, not FRED history."""
    resp = client().request(AccountInstruments(accountID=os.environ["OANDA_ACCOUNT_ID"], params={"instruments": instrument}))
    fin = resp["instruments"][0]["financing"]
    long_rate, short_rate = float(fin["longRate"]), float(fin["shortRate"])
    if long_rate >= short_rate:
        return 1, long_rate
    return -1, short_rate


def current_price_and_weight(instrument: str) -> tuple[float, float]:
    df = fetch_daily(instrument, count=30)
    if len(df) < 20:
        raise RuntimeError(f"not enough recent history for {instrument}")
    price = float(df["close"].iloc[-1])
    a = atr(df)
    daily_vol_frac = max(float(a.iloc[-1]) / price, 1e-6)
    weight = min(TARGET_DAILY_VOL / daily_vol_frac, MAX_POSITION_WEIGHT)
    return price, weight


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def append_ledger(row: dict) -> None:
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(row) + "\n")


def write_performance_summary() -> None:
    """Recomputes CAGR/Sharpe/max_dd from the ledger so far — will be
    thin/noisy early on (same as the FX signal product's shadow mode
    starting from n=1), that's expected, not a bug."""
    if not LEDGER_PATH.exists():
        return
    rows = [json.loads(line) for line in LEDGER_PATH.read_text().splitlines() if line.strip()]
    if not rows:
        return
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])

    per_pair = {}
    for pair, group in df.groupby("pair"):
        series = group.set_index("date")["net_return"].sort_index()
        per_pair[pair] = stats(series, pair)

    pivot = df.pivot_table(index="date", columns="pair", values="net_return", aggfunc="sum")
    n_active = pivot.notna().sum(axis=1).clip(lower=1)
    portfolio_returns = pivot.fillna(0).sum(axis=1) / n_active
    portfolio = stats(portfolio_returns, "PORTFOLIO")

    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "config": "JPY-crosses carry, vol-targeted sizing, real OANDA live financing rates",
        "days_tracked": int(pivot.shape[0]),
        "per_pair": per_pair,
        "portfolio": portfolio,
    }
    tmp = PERFORMANCE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=float))
    tmp.replace(PERFORMANCE_PATH)


def main() -> None:
    state = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for instrument in PAIRS:
        try:
            direction, rate = live_rate_and_direction(instrument)
            price, weight = current_price_and_weight(instrument)
        except Exception as ex:
            print(f"  {instrument}: error — {ex}")
            continue

        prev = state.get(instrument)
        if prev is None:
            # first run — establish baseline, no return to record yet
            state[instrument] = {"direction": direction, "price": price, "weight": weight,
                                 "rate": rate, "last_update": today}
            print(f"  {instrument}: baseline established — direction={'long' if direction==1 else 'short'} rate={rate*100:+.2f}%/yr")
            continue

        if prev.get("last_update") == today:
            continue  # already ran today, don't double-count

        price_return = (price - prev["price"]) / prev["price"]
        held_direction = prev["direction"]
        held_weight = prev["weight"]
        price_pnl = held_direction * held_weight * price_return
        financing_pnl = held_weight * (prev["rate"] / 365.0)

        flipped = direction != held_direction
        cost = (SPREADS[instrument] / price) * held_weight if flipped else 0.0

        net_return = price_pnl + financing_pnl - cost

        append_ledger({
            "date": today, "pair": instrument, "direction": held_direction,
            "price": price, "net_return": net_return, "price_pnl": price_pnl,
            "financing_pnl": financing_pnl, "cost": cost, "flipped": flipped,
            "new_direction": direction,
        })
        flip_note = f" — FLIPPED to {'long' if direction==1 else 'short'}" if flipped else ""
        print(f"  {instrument}: net_return={net_return:+.5f} (price={price_pnl:+.5f} financing={financing_pnl:+.5f} cost={cost:+.5f}){flip_note}")

        state[instrument] = {"direction": direction, "price": price, "weight": weight,
                             "rate": rate, "last_update": today}

    save_state(state)
    write_performance_summary()


if __name__ == "__main__":
    main()
