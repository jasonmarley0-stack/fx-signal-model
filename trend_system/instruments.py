"""Universe for the trend-following prototype — deliberately diversified
across asset classes (currencies, precious metals, equity indices), not
just FX majors, since diversification across genuinely uncorrelated
markets is the actual edge-generation mechanism this system is testing
(see trend_system/README.md).

Spreads are a live OANDA snapshot (2026-09-25), same convention as
src/spreads.py: used to floor/model real trading cost, not just backtest
gross returns.

raw_instrument is the literal OANDA instrument code. Only FX pairs follow
the "3+3 letters" convention data.oanda.to_oanda_instrument() assumes —
XAU_USD, SPX500_USD etc. don't, so this system fetches candles directly
against the raw code rather than reusing that FX-specific helper.
"""

UNIVERSE = {
    # FX majors
    "EUR_USD": {"class": "fx", "spread": 0.000370},
    "GBP_USD": {"class": "fx", "spread": 0.000750},
    "USD_JPY": {"class": "fx", "spread": 0.057000},
    "USD_CHF": {"class": "fx", "spread": 0.000920},
    "AUD_USD": {"class": "fx", "spread": 0.001130},
    "USD_CAD": {"class": "fx", "spread": 0.000990},
    "NZD_USD": {"class": "fx", "spread": 0.001590},
    # Precious metals
    "XAU_USD": {"class": "metal", "spread": 19.560000},
    "XAG_USD": {"class": "metal", "spread": 0.122000},
    # Equity indices
    "SPX500_USD": {"class": "equity_index", "spread": 4.700000},
    "NAS100_USD": {"class": "equity_index", "spread": 11.000000},
    "US30_USD": {"class": "equity_index", "spread": 28.000000},
}
