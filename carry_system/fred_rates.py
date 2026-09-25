"""Real historical short-term interest rates per currency, from FRED (St.
Louis Fed) — replaces the "today's rate held constant" approximation in
the first carry backtest with actual point-in-time rate history, so the
carry differential on any given historical date is the real one, not a
guess (see NEXT_STEPS.md, carry backtest v1's disclosed limitation).

Series chosen per currency (short-term/policy rate proxies, not long bond
yields — carry is driven by SHORT rates): each one checked for current
data (updated into 2026) before selecting, since some FRED series for
smaller economies stop updating years before "now" without warning (found
this the hard way — Canada's first-choice series stopped in Dec 2023).

Central bank rates are step functions in reality (they only change at
scheduled decisions, not daily) — forward-filling between FRED
observations to build a daily series is the *correct* way to represent
that, not an approximation.
"""
from __future__ import annotations
import os
import requests
import pandas as pd

FRED_SERIES = {
    "USD": "DFF",              # Federal Funds Effective Rate, daily
    "EUR": "ECBMRRFR",         # ECB Main Refinancing Rate, daily
    "GBP": "IR3TIB01GBM156N",  # UK 3-month interbank, monthly
    "JPY": "IRSTCI01JPM156N",  # Japan call money/interbank, monthly
    "CHF": "IR3TIB01CHM156N",  # Switzerland 3-month interbank, monthly
    "AUD": "IR3TIB01AUM156N",  # Australia 3-month interbank, monthly
    "CAD": "IRSTCI01CAM156N",  # Canada call money/interbank, monthly
    "NZD": "IR3TIB01NZM156N",  # New Zealand 3-month interbank, monthly
}


def fetch_fred_series(series_id: str) -> pd.Series:
    api_key = os.environ["FRED_API_KEY"]
    resp = requests.get("https://api.stlouisfed.org/fred/series/observations", params={
        "series_id": series_id, "api_key": api_key, "file_type": "json",
    }, timeout=30)
    resp.raise_for_status()
    obs = resp.json()["observations"]
    dates, values = [], []
    for o in obs:
        if o["value"] == ".":  # FRED's missing-value marker
            continue
        dates.append(o["date"])
        values.append(float(o["value"]))
    s = pd.Series(values, index=pd.to_datetime(dates, utc=True))
    return s.sort_index()


def load_all_rates() -> dict[str, pd.Series]:
    """Returns {currency: daily rate series (%, forward-filled from actual
    change dates)} for all 8 currencies."""
    rates = {}
    for ccy, series_id in FRED_SERIES.items():
        s = fetch_fred_series(series_id)
        # reindex to a full daily calendar and forward-fill — real rate stays
        # in effect until the next actual change, exactly as it does in reality
        full_index = pd.date_range(s.index.min(), s.index.max(), freq="D", tz="UTC")
        rates[ccy] = s.reindex(full_index).ffill()
    return rates


def pair_rate_diff(rates: dict[str, pd.Series], base: str, quote: str) -> pd.Series:
    """Annual rate differential (%) for holding base long / quote short,
    aligned on dates both currencies have data for."""
    b, q = rates[base], rates[quote]
    common_index = b.index.intersection(q.index)
    return (b.loc[common_index] - q.loc[common_index]) / 100.0  # as a fraction, matching backtest.py's convention


if __name__ == "__main__":
    rates = load_all_rates()
    for ccy, s in rates.items():
        print(f"{ccy}: {s.index.min().date()} to {s.index.max().date()}, "
              f"{len(s)} days, latest={s.iloc[-1]:.2f}%")
