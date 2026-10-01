"""EMA(20/50) regime + MACD momentum trend-following / filter strategy."""
import pandas as pd
from .indicators import ema, macd


def trend_signal(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> pd.Series:
    """Returns a score in {-1, 0, 1}.

    +1: EMA_fast > EMA_slow AND MACD histogram positive and rising
    -1: mirror image
     0: mixed / no clear regime
    """
    close = df["close"]
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    _, _, hist = macd(close)
    hist_rising = hist.diff() > 0

    bullish = (ema_fast > ema_slow) & (hist > 0) & hist_rising
    bearish = (ema_fast < ema_slow) & (hist < 0) & ~hist_rising

    score = pd.Series(0, index=df.index, dtype=float)
    score[bullish] = 1.0
    score[bearish] = -1.0
    return score
