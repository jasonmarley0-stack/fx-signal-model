"""Crypto perpetual funding-rate harvesting — long spot, short the
perpetual future, collect the funding payment (paid every 8h on Binance)
whenever it's positive. This is delta-neutral by construction: the spot
long and perp short cancel out price exposure, so the return is the
funding payment itself, not a directional bet — a genuinely different
mechanism from everything else tested so far.

Deliberately scoped to the easier, more honestly-executable half of the
trade: ON (long spot + short perp) when funding is positive, FLAT when
it's negative — not flipping to the reverse (short spot + long perp),
which needs spot margin-borrowing infrastructure with its own cost this
backtest isn't modeling. That's a real, disclosed simplification, not a
hidden one — it means this only captures the "usual" side of the trade
(perpetuals have a structural tendency to trade at a premium, so positive
funding has historically been the common case), and probably understates
what a fully two-sided version could earn.

Cost model, corrected 2026-09-25 after the first pass (Sharpe ~2) drew
scrutiny — rightly, since this position flips on/off a lot (13.1% of all
periods for BTC, 300 of 506 negative-funding runs are a single isolated
8h blip), making the result unusually sensitive to the exact cost
assumption:
  - Fees: checked Binance's real published standard (non-VIP) taker
    schedule — spot 0.10%, USDT-M futures 0.05% — round trip on both legs
    (open+close spot, open+close perp) = 2x0.10% + 2x0.05% = 0.30%, not
    the 0.08% first assumed (~3.75x higher).
  - Spread: checked live via Binance's public book-ticker endpoint —
    genuinely tiny for BTC/ETH (spot ~0.001%, perp ~0.01%), confirmed
    negligible next to the fee cost rather than assumed away.
  - Persistence filter: added MIN_PERSISTENCE — don't flip state until
    the new sign has held for that many consecutive periods, so a single
    negative 8h blip surrounded by positive periods doesn't trigger a
    full exit+re-entry (paying the ~0.30% round trip) to avoid one small
    negative accrual. Same debounce principle as the FX signal work
    months ago, applied to time-persistence instead of score magnitude.
"""
from __future__ import annotations
import time
import requests
import pandas as pd

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
ROUND_TRIP_COST = 0.0030  # real fee schedule + measured spread — see module docstring
MIN_PERSISTENCE = 2  # consecutive periods the new sign must hold before actually flipping
FUNDING_INTERVALS_PER_YEAR = 365 * 3  # 8h funding, 3x/day


def fetch_all_funding(symbol: str) -> pd.Series:
    """Paginates through Binance's funding-rate history to get the full
    series, not just the most recent page.

    Bug fixed 2026-09-25: omitting startTime on the first request doesn't
    return the oldest data — it returns only a recent window (~500
    events, ~6 months), and the old exit condition (`len(rows) < 1000`)
    then stopped immediately, silently truncating the whole history to 6
    months. Confirmed via direct curl that startTime=2017-01-01 correctly
    returns data back to Sept 2019 (BTCUSDT's real listing date for
    perpetuals) — seeding an explicit old startTime is required, not
    optional."""
    all_rows = []
    start_time = 1483228800000  # 2017-01-01 UTC — before any of these symbols existed, so page from the true start
    while True:
        params = {"symbol": symbol, "limit": 1000, "startTime": start_time}
        resp = requests.get("https://fapi.binance.com/fapi/v1/fundingRate", params=params, timeout=30)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        all_rows.extend(rows)
        if len(rows) < 1000:
            break
        start_time = rows[-1]["fundingTime"] + 1
        time.sleep(0.2)  # light pacing, not a rate-limit workaround — polite to a free public endpoint
    s = pd.Series(
        {pd.to_datetime(r["fundingTime"], unit="ms", utc=True): float(r["fundingRate"]) for r in all_rows}
    )
    return s.sort_index()


def stats(returns: pd.Series, periods_per_year: int, label: str) -> dict:
    if returns.empty or returns.std() == 0:
        return {"label": label, "cagr": None, "sharpe": None, "max_dd": None, "total_return": None}
    equity = (1 + returns).cumprod()
    years = len(returns) / periods_per_year
    total_return = equity.iloc[-1] - 1
    cagr = equity.iloc[-1] ** (1 / years) - 1 if years > 0 else None
    sharpe = (returns.mean() / returns.std()) * (periods_per_year ** 0.5) if returns.std() > 0 else None
    running_max = equity.cummax()
    max_dd = ((equity - running_max) / running_max).min()
    return {"label": label, "total_return": total_return, "cagr": cagr, "sharpe": sharpe, "max_dd": max_dd, "years": years}


def print_stats(s: dict) -> None:
    if s["cagr"] is None:
        print(f"{s['label']:20} insufficient data")
        return
    print(f"{s['label']:20} years={s['years']:.1f} total_return={s['total_return']*100:+.1f}% "
          f"CAGR={s['cagr']*100:+.2f}% sharpe={s['sharpe']:.2f} max_dd={s['max_dd']*100:.1f}%")


def walk_with_persistence(funding: pd.Series) -> pd.Series:
    """Debounced version of 'on = funding > 0' — requires the new sign to
    hold for MIN_PERSISTENCE consecutive periods before actually flipping
    state, so isolated single-period blips don't trigger a full round-trip
    exit+re-entry. Costed on the state actually held, not the raw sign."""
    net_return = pd.Series(0.0, index=funding.index)
    state_on = False
    pending_sign = None
    pending_run = 0
    for t, rate in funding.items():
        sign_positive = rate > 0
        if sign_positive == state_on:
            pending_run = 0  # already in the matching state, nothing pending
        else:
            if pending_sign == sign_positive:
                pending_run += 1
            else:
                pending_sign, pending_run = sign_positive, 1
            if pending_run >= MIN_PERSISTENCE:
                net_return.loc[t] -= ROUND_TRIP_COST  # pay the flip cost on the period the switch actually happens
                state_on = sign_positive
                pending_sign, pending_run = None, 0
        if state_on:
            net_return.loc[t] += rate
    return net_return


def main() -> None:
    per_symbol = {}
    for symbol in SYMBOLS:
        print(f"Fetching full funding-rate history for {symbol}...")
        funding = fetch_all_funding(symbol)
        print(f"  {len(funding)} funding events, {funding.index.min().date()} to {funding.index.max().date()}")
        print(f"  positive funding: {(funding > 0).mean()*100:.1f}% of periods, "
              f"mean when positive={funding[funding>0].mean()*FUNDING_INTERVALS_PER_YEAR*100:.1f}%/yr annualized")

        net_return = walk_with_persistence(funding)

        per_symbol[symbol] = net_return
        print_stats(stats(net_return, FUNDING_INTERVALS_PER_YEAR, f"{symbol} net"))
        print()

    combined = pd.concat(per_symbol, axis=1)
    n_active = combined.notna().sum(axis=1).clip(lower=1)
    portfolio = combined.fillna(0).sum(axis=1) / n_active
    print("=== PORTFOLIO (equal-weighted across symbols) ===")
    print_stats(stats(portfolio, FUNDING_INTERVALS_PER_YEAR, "PORTFOLIO"))


if __name__ == "__main__":
    main()
