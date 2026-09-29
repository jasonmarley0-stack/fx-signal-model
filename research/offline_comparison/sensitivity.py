"""Cost and execution-delay sensitivity: re-scores the SAME issued
versions the main replay already produced (no new signal generation),
under two alternate assumptions, so the delta is attributable only to
that one changed assumption:

  - cost: fills priced at MID instead of the real bid/ask crossing (an
    idealised, zero-spread comparison against the main run's real-cost
    result).
  - delay: the entry window is shifted to open one extra M30 bar later
    (an additional 30 minutes of execution delay beyond the contract's
    normal next-instant availability).
"""
from __future__ import annotations
from datetime import timedelta
import pandas as pd
from replay_scorer import score_replay_version


def _mid(df_bid: pd.DataFrame, df_ask: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"open": (df_bid["open"] + df_ask["open"]) / 2, "high": (df_bid["high"] + df_ask["high"]) / 2,
                          "low": (df_bid["low"] + df_ask["low"]) / 2, "close": (df_bid["close"] + df_ask["close"]) / 2},
                         index=df_bid.index)


def rescored_with_mid(issued_rows: list[dict], pair_data_by_pair: dict, dataset_end) -> list[dict]:
    out = []
    for r in issued_rows:
        pd_ = pair_data_by_pair[r["pair"]]
        mid = _mid(pd_.m30_bid, pd_.m30_ask)
        version = {"pair": r["pair"], "direction": r["direction"], "entry": r["entry_price"],
                   "entry_condition_lo": r["entry_condition_lo"], "entry_condition_hi": r["entry_condition_hi"],
                   "stop": r["stop"], "target": r["target"],
                   "published_at": pd.Timestamp(r["recorded_at_utc"]).to_pydatetime(),
                   "max_holding_time_hours": r["max_holding_time_hours"]}
        window_end = pd.Timestamp(r["entry_expiry_utc"]).to_pydatetime()
        scored = score_replay_version(version, window_end, "entry_expiry", mid, mid, now=dataset_end)
        out.append({**r, **scored})
    return out


def rescored_with_extra_delay(issued_rows: list[dict], pair_data_by_pair: dict, dataset_end,
                               extra_minutes: int = 30) -> list[dict]:
    out = []
    for r in issued_rows:
        pd_ = pair_data_by_pair[r["pair"]]
        version = {"pair": r["pair"], "direction": r["direction"], "entry": r["entry_price"],
                   "entry_condition_lo": r["entry_condition_lo"], "entry_condition_hi": r["entry_condition_hi"],
                   "stop": r["stop"], "target": r["target"],
                   "published_at": pd.Timestamp(r["recorded_at_utc"]).to_pydatetime() + timedelta(minutes=extra_minutes),
                   "max_holding_time_hours": r["max_holding_time_hours"]}
        window_end = pd.Timestamp(r["entry_expiry_utc"]).to_pydatetime() + timedelta(minutes=extra_minutes)
        scored = score_replay_version(version, window_end, "entry_expiry", pd_.m30_bid, pd_.m30_ask, now=dataset_end)
        out.append({**r, **scored})
    return out
