"""Carry, narrowed to the JPY-crosses that showed stable, positive results
in backtest_real_rates.py (1 direction flip in 20 years each — JPY has
been the structural low-yielder almost the entire period) — dropping
USD/CHF, GBP/USD, USD/CAD, AUD/CHF, NZD/CHF, which had unstable rate
relationships (35-47 flips) and weaker/negative results.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "trend_system"))

import backtest_real_rates as base  # noqa: E402

base.PAIRS = [p for p in base.PAIRS if p[0].endswith("_JPY")]

if __name__ == "__main__":
    base.main()
