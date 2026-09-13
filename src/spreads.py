"""Real bid/ask spreads, used to floor stop-loss distance against the actual
cost of trading (see combiner.py) — an ATR-based stop with no reference to
spread was found to make most trades unprofitable by construction (spread
alone exceeded the entire stop-loss risk on ~78% of live signals; see
NEXT_STEPS.md, "transaction cost modeling").

Values are a live snapshot from the OANDA practice account (2026-09-11), in
the same mid-price units as entry/stop levels. Not a full intraday-varying
model — real spread widens in thin liquidity and around news — but a
realistic floor, and a large improvement over assuming zero cost.

Revisit periodically: pull fresh values via oandapyV20's PricingInfo
endpoint rather than assuming these stay accurate indefinitely.
"""

SPREADS = {
    "EURUSD": 0.000370, "GBPUSD": 0.000750, "USDJPY": 0.057000, "USDCHF": 0.000920,
    "AUDUSD": 0.001130, "USDCAD": 0.000990, "NZDUSD": 0.001590, "EURGBP": 0.000750,
    "EURJPY": 0.152000, "EURCHF": 0.000970, "EURAUD": 0.002880, "EURCAD": 0.001980,
    "EURNZD": 0.003330, "GBPJPY": 0.222000, "GBPCHF": 0.001950, "GBPAUD": 0.003400,
    "GBPCAD": 0.002290, "GBPNZD": 0.004410, "AUDJPY": 0.244000, "AUDCHF": 0.001150,
    "AUDCAD": 0.002530, "AUDNZD": 0.002180, "NZDJPY": 0.301000, "NZDCHF": 0.001280,
    "NZDCAD": 0.002650, "CADJPY": 0.176000, "CADCHF": 0.001100, "CHFJPY": 0.280000,
    "XAU_USD": 20.560000, "SPX500_USD": 4.700000,
}


def get_spread(pair: str) -> float:
    """Raises on an unknown pair rather than silently falling back to a
    guessed default — a missing spread is a configuration gap that should
    fail loudly, not quietly under-price risk."""
    try:
        return SPREADS[pair]
    except KeyError:
        raise KeyError(
            f"No known spread for {pair!r} — add it to src/spreads.py (fetch via "
            f"oandapyV20's PricingInfo) before trading it. Refusing to guess."
        )
